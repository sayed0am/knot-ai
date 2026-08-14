"""Enforce the knot layering rule: a module may only import from its own
layer or a strictly lower one.

Layer order (low -> high): providers < core < authoring < server.
Modules directly under ``knot/`` (e.g. ``knot/__init__.py``) sit below every
layer and may not import any of them, keeping ``import knot`` cheap.

This module exposes ``LAYER_ORDER`` as a reusable constant so CI or other
tooling can import the same source of truth instead of duplicating it.
"""

import ast
from pathlib import Path

# Layer name -> rank. Lower rank = lower layer = fewer allowed imports.
LAYER_ORDER: dict[str, int] = {
    "providers": 0,
    "core": 1,
    "authoring": 2,
    "server": 3,
}

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "knot"


def _layer_of(py_file: Path) -> str | None:
    """Return the knot layer a file belongs to, or None for root-level files."""
    rel_parts = py_file.relative_to(SRC_ROOT).parts
    # rel_parts[0] is either a layer package name or a root-level file
    # (e.g. "__init__.py").
    first = rel_parts[0]
    if first in LAYER_ORDER:
        return first
    return None


def _knot_imports(py_file: Path) -> list[str]:
    """Return every dotted 'knot.*' module path this file imports.

    Relative imports are resolved against the file's own package so that an
    upward import written relatively (e.g. ``from ..server import app`` inside
    ``core/``) is still caught.
    """
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    rel_parts = py_file.relative_to(SRC_ROOT.parent).parts  # ("knot", "core", "foo.py")
    package_parts = list(rel_parts[:-1])
    if rel_parts[-1] != "__init__.py":
        package_parts.append(rel_parts[-1].removesuffix(".py"))
        # for a plain module, level-1 relative imports resolve against its parent
        # package; ast levels count from the module, so drop the module name
        package_parts = package_parts[:-1]

    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "knot" or alias.name.startswith("knot."):
                    targets.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                base = package_parts[: len(package_parts) - (node.level - 1)]
                resolved = base + (node.module.split(".") if node.module else [])
                dotted = ".".join(resolved)
                if dotted == "knot" or dotted.startswith("knot."):
                    targets.append(dotted)
            elif node.module and (node.module == "knot" or node.module.startswith("knot.")):
                targets.append(node.module)
    return targets


def _target_layer(module_path: str) -> str | None:
    """Given 'knot.core.foo.bar', return 'core', or None if it targets the root."""
    parts = module_path.split(".")
    if len(parts) < 2:
        return None
    candidate = parts[1]
    return candidate if candidate in LAYER_ORDER else None


def test_import_direction():
    py_files = sorted(SRC_ROOT.rglob("*.py"))
    assert py_files, f"expected to find .py files under {SRC_ROOT}"

    violations: list[str] = []

    for py_file in py_files:
        own_layer = _layer_of(py_file)
        own_rank = LAYER_ORDER[own_layer] if own_layer is not None else -1

        for imported in _knot_imports(py_file):
            target_layer = _target_layer(imported)
            if target_layer is None:
                # Import of the bare 'knot' package or another root-level
                # module carries no layer, so it can't violate ordering.
                continue
            target_rank = LAYER_ORDER[target_layer]

            if own_layer is None:
                # Root-level modules (e.g. knot/__init__.py) may not import
                # any layer at all.
                violations.append(
                    f"{py_file.relative_to(SRC_ROOT.parent.parent)}: "
                    f"root-level module imports '{imported}' "
                    f"(layer '{target_layer}'), but root modules may not "
                    f"import any layer"
                )
            elif target_rank > own_rank:
                violations.append(
                    f"{py_file.relative_to(SRC_ROOT.parent.parent)}: "
                    f"module in layer '{own_layer}' imports '{imported}' "
                    f"from higher layer '{target_layer}' "
                    f"(order: {' < '.join(sorted(LAYER_ORDER, key=LAYER_ORDER.get))})"
                )

    assert not violations, "import-direction violations found:\n" + "\n".join(
        f"  - {v}" for v in violations
    )
