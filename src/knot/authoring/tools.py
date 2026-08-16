"""The ``@tool`` decorator and its compile-time schema derivation.

An authored tool is a single Python file with exactly one decorated
function. The tool's *name* is the module's file stem (``tools/get_invoice.py``
becomes ``get_invoice``) unless the decorator is given an explicit
``name=`` override; its JSON schema is computed from the function's
signature via pydantic, and its description from the first paragraph of its
docstring.

This module never touches the filesystem beyond ``load_tool_module``, which
imports one file in isolation (a unique module name, no ``sys.path``
mutation, no ``sys.modules`` registration) so authored tool files can never
collide with each other or with the host process's own modules.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, get_args, get_type_hints

from pydantic import ConfigDict, Field, create_model
from pydantic.fields import FieldInfo

from knot.authoring.discovery import ID_PATTERN
from knot.core.tools import AgentTool, AgentToolResult
from knot.providers.messages import TextContent

_MARKER_ATTR = "__knot_tool_marker__"


class ToolDefinitionError(Exception):
    """A tool module could not be turned into an :class:`AgentTool`.

    Raised for both authoring mistakes (zero or multiple decorated
    functions, ``*args``/``**kwargs``, missing annotations, an invalid
    name) and left uncaught so callers (``knot.authoring.compile``) can
    catch it uniformly alongside plain import errors and turn it into a
    per-module diagnostic.
    """


@dataclass(frozen=True, slots=True)
class ToolMarker:
    """Metadata ``@tool`` attaches to the decorated function."""

    func: Callable[..., Any]
    idempotent: bool
    name_override: str | None


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    idempotent: bool = False,
    name: str | None = None,
) -> Any:
    """Mark a function as the one tool exposed by its module.

    Usable bare (``@tool``) or with keyword arguments (``@tool(idempotent=True)``).
    The function itself is returned unmodified (aside from an attached
    marker) so it stays directly callable and testable outside the
    framework.
    """

    def decorate(target: Callable[..., Any]) -> Callable[..., Any]:
        setattr(
            target,
            _MARKER_ATTR,
            ToolMarker(func=target, idempotent=idempotent, name_override=name),
        )
        return target

    if fn is not None:
        return decorate(fn)
    return decorate


def load_tool_module(path: Path) -> ModuleType:
    """Import one authored tool file in isolation.

    Uses a freshly generated module name via ``spec_from_file_location`` so
    two agents' identically-named ``tools/*.py`` files never collide, and
    intentionally does not register the module under ``sys.modules`` —
    nothing outside this one compile step should ever see it.
    """
    module_name = f"_knot_authored_tool_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load tool module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_tool_marker(module: ModuleType) -> ToolMarker:
    """Return the single ``@tool``-decorated function defined *in* this module.

    Only functions whose ``__module__`` matches (i.e. defined in this file,
    not merely imported into it) are considered.
    """
    markers = [
        getattr(obj, _MARKER_ATTR)
        for obj in vars(module).values()
        if inspect.isfunction(obj)
        and getattr(obj, "__module__", None) == module.__name__
        and hasattr(obj, _MARKER_ATTR)
    ]
    if not markers:
        raise ToolDefinitionError("module defines no @tool-decorated function")
    if len(markers) > 1:
        names = ", ".join(sorted(m.func.__name__ for m in markers))
        raise ToolDefinitionError(f"module defines more than one @tool-decorated function: {names}")
    return markers[0]


def _first_paragraph(doc: str) -> str:
    doc = doc.strip()
    if not doc:
        return ""
    first = doc.split("\n\n", 1)[0]
    return " ".join(line.strip() for line in first.splitlines() if line.strip())


def _split_annotated(annotation: Any) -> tuple[Any, str | None]:
    """Return ``(base_type, description)``, unwrapping ``Annotated[T, "..."]``.

    ``typing.Annotated`` doesn't expose a stable ``get_origin`` sentinel
    across the metadata it wraps, so detect it structurally via the
    ``__metadata__`` attribute it stamps onto its result instead.
    """
    metadata = getattr(annotation, "__metadata__", None)
    if metadata is None:
        return annotation, None
    args = get_args(annotation)
    base = args[0] if args else annotation
    description = next((meta for meta in metadata if isinstance(meta, str)), None)
    return base, description


def _field_spec(annotation: Any, default: Any) -> tuple[Any, Any]:
    base, description = _split_annotated(annotation)
    if isinstance(default, FieldInfo):
        return base, default
    if description is not None:
        return base, Field(default=default, description=description)
    return base, default


def build_agent_tool(marker: ToolMarker, *, file_stem: str) -> AgentTool:
    """Derive an :class:`AgentTool` from a marked function's signature."""
    fn = marker.func
    signature = inspect.signature(fn)
    hints = get_type_hints(fn, include_extras=True)

    fields: dict[str, tuple[Any, Any]] = {}
    for param_name, param in signature.parameters.items():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            raise ToolDefinitionError(
                f"parameter {param_name!r} uses *args/**kwargs, which @tool does not support"
            )
        if param_name not in hints:
            raise ToolDefinitionError(f"parameter {param_name!r} is missing a type annotation")
        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[param_name] = _field_spec(hints[param_name], default)

    name = marker.name_override if marker.name_override is not None else file_stem
    if not ID_PATTERN.match(name):
        raise ToolDefinitionError(
            f"tool name {name!r} is invalid: must match {ID_PATTERN.pattern!r}"
        )

    args_model = create_model(
        f"{fn.__name__}_Arguments", __config__=ConfigDict(extra="forbid"), **fields
    )
    schema = args_model.model_json_schema()
    schema.pop("title", None)

    description = _first_paragraph(inspect.getdoc(fn) or "")
    is_async = inspect.iscoroutinefunction(fn)

    async def execute_fn(
        tool_call_id: str,
        arguments: Any,
        signal: Any = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        # A validation error here is intentionally *not* caught: it propagates
        # to knot.core.tools.execute_tool, whose own try/except converts any
        # exception raised by an executor into an error result. That keeps
        # "invalid arguments" and "the tool itself raised" on one code path.
        validated = args_model(**dict(arguments))
        kwargs = validated.model_dump()
        if is_async:
            raw = await fn(**kwargs)
        else:
            raw = await asyncio.to_thread(fn, **kwargs)
        return _coerce_result(raw)

    return AgentTool(
        name=name,
        description=description,
        parameters=schema,
        execute_fn=execute_fn,
        idempotent=marker.idempotent,
    )


def _coerce_result(value: Any) -> AgentToolResult:
    if isinstance(value, AgentToolResult):
        return value
    if value is None:
        return AgentToolResult(content=[])
    if isinstance(value, str):
        return AgentToolResult(content=[TextContent(text=value)])
    return AgentToolResult(content=[TextContent(text=str(value))])


def compile_tool_module(path: Path) -> AgentTool:
    """Import ``path`` and turn its one ``@tool`` function into an :class:`AgentTool`.

    The tool's name comes from ``path``'s file stem unless overridden by
    ``@tool(name=...)``. Any exception raised while importing or inspecting
    the module (including :class:`ToolDefinitionError`) propagates to the
    caller, which is expected to be the compile phase's per-module
    diagnostic boundary.
    """
    module = load_tool_module(path)
    marker = find_tool_marker(module)
    return build_agent_tool(marker, file_stem=path.stem)


__all__ = [
    "ToolDefinitionError",
    "ToolMarker",
    "build_agent_tool",
    "compile_tool_module",
    "find_tool_marker",
    "load_tool_module",
    "tool",
]
