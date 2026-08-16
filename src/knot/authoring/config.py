"""Schemas for authored ``agent.yaml`` and ``bundle.yaml`` files.

These are strict (``extra="forbid"``) so a typo'd key is caught at compile
time instead of silently doing nothing. Loading never raises: parse and
validation failures are wrapped into a :class:`~knot.authoring.discovery.Diagnostic`
and returned alongside a ``None`` config, so a broken config file fails only
the one agent or bundle that owns it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from knot.authoring.discovery import Diagnostic
from knot.providers.messages import WireModel

ApprovalPolicyName = Literal["never", "once", "always"]


class _StrictModel(BaseModel):
    """Local base for author-facing YAML schemas: strict, snake_case only.

    Unlike ``knot.providers.messages.WireModel``, these are never serialized
    to a wire protocol, so there is no camelCase alias generator here — the
    YAML key an author writes is exactly the Python field name.
    """

    model_config = ConfigDict(extra="forbid")


class ModelConfig(WireModel):
    """Embedded verbatim into ``AgentManifest.model`` (see ``manifest.py``),
    so this is a ``WireModel`` rather than ``_StrictModel``: the manifest is
    a frontend-facing artifact and must serialize uniformly camelCase.
    ``WireModel``'s ``validate_by_name=True`` still accepts the plain
    snake_case keys an author writes in ``agent.yaml`` unchanged — only
    serialization is affected."""

    provider: Literal["anthropic", "openai", "openrouter", "litellm"]
    name: str
    max_tokens: int | None = None
    # Overrides `knot.providers.capacity`'s built-in defaults table for this
    # model's context window (used by the proactive compaction trigger).
    # `None` (the default) falls back to the table; an unmapped model with
    # no override just leaves proactive compaction disabled for that model.
    context_window: int | None = None

    @field_validator("context_window")
    @classmethod
    def _context_window_must_be_positive(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("model.context_window must be a positive integer")
        return value


class CompactionConfig(WireModel):
    """Embedded verbatim into ``AgentManifest.compaction``; see
    ``ModelConfig`` for why this is a ``WireModel``.

    Defaults enable compaction with no configuration at all (spec: "Zero-
    config default"). ``retain_budget`` must stay strictly below
    ``threshold_ratio`` — otherwise a proactive compaction would retain a
    tail that alone already exceeds the trigger threshold, immediately
    re-triggering next turn.
    """

    enabled: bool = True
    threshold_ratio: float = 0.8
    retain_budget: float = 0.16
    summarization_model: str | None = None
    max_overflow_retries: int = 1
    summarization_max_tokens: int = 8192

    @field_validator("threshold_ratio")
    @classmethod
    def _threshold_ratio_range(cls, value: float) -> float:
        if not (0 < value <= 1):
            raise ValueError("compaction.threshold_ratio must be within (0, 1]")
        return value

    @field_validator("retain_budget")
    @classmethod
    def _retain_budget_range(cls, value: float) -> float:
        if not (0 < value < 1):
            raise ValueError("compaction.retain_budget must be within (0, 1)")
        return value

    @field_validator("max_overflow_retries")
    @classmethod
    def _max_overflow_retries_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("compaction.max_overflow_retries must be >= 0")
        return value

    @field_validator("summarization_max_tokens")
    @classmethod
    def _summarization_max_tokens_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("compaction.summarization_max_tokens must be a positive integer")
        return value

    @model_validator(mode="after")
    def _retain_budget_below_threshold(self) -> CompactionConfig:
        if self.retain_budget >= self.threshold_ratio:
            raise ValueError(
                "compaction.retain_budget must be less than compaction.threshold_ratio"
            )
        return self


class LimitsConfig(WireModel):
    """Embedded verbatim into ``AgentManifest.limits``; see ``ModelConfig``
    for why this is a ``WireModel``."""

    max_turns: int | None = None
    max_result_bytes: int | None = None
    delegation_max_per_turn: int = 4
    delegation_max_concurrent: int = 2


class AgentConfig(_StrictModel):
    """Schema for ``agent.yaml``. Every field is optional: an agent needs no
    ``agent.yaml`` at all to compile."""

    description: str | None = None
    model: ModelConfig | None = None
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    use: list[str] = Field(default_factory=list)
    approvals: dict[str, ApprovalPolicyName] = Field(default_factory=dict)


class ConnectionConfig(_StrictModel):
    """One MCP-style connection declared in ``bundle.yaml``.

    Only HTTP transports are supported: connections are resolved from a
    committed offline snapshot (see ``knot.authoring.compile``), and a
    ``stdio`` server has no meaningful way to be snapshotted or reached
    without a live subprocess, so it must be wrapped as an HTTP service
    first.
    """

    url: str
    transport: Literal["streamable_http", "sse"]
    description: str = ""
    allow: list[str] | None = None
    auth_env: str | None = None
    provided_arguments: dict[str, str] = Field(default_factory=dict)

    @field_validator("transport", mode="before")
    @classmethod
    def _reject_non_http_transports(cls, value: object) -> object:
        if value == "stdio":
            raise ValueError(
                "transport 'stdio' is not supported; only HTTP transports "
                "('streamable_http', 'sse') are allowed here — wrap the "
                "stdio server as an HTTP service and connect to that instead"
            )
        return value

    @field_validator("allow")
    @classmethod
    def _reject_qualified_allow_entries(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        for name in value:
            if "__" in name:
                raise ValueError(
                    f"allow entry {name!r} must be a plain tool name as the server "
                    "reports it (no '__') — the '<connection>__<tool>' qualification "
                    "is applied automatically at compile time"
                )
        return value


class BundleConfig(_StrictModel):
    """Schema for ``bundle.yaml``."""

    description: str | None = None
    approvals: dict[str, ApprovalPolicyName] = Field(default_factory=dict)
    connections: dict[str, ConnectionConfig] = Field(default_factory=dict)


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        loc = ".".join(str(p) for p in error["loc"]) or "<root>"
        parts.append(f"{loc}: {error['msg']}")
    return "; ".join(parts)


def load_agent_config(path: Path, *, agent_id: str) -> tuple[AgentConfig | None, list[Diagnostic]]:
    """Parse and validate an ``agent.yaml`` file.

    Returns ``(config, [])`` on success, or ``(None, [error diagnostic])`` on
    any YAML or validation failure. Never raises.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, [
            Diagnostic(
                severity="error", path=path, message=f"invalid YAML: {exc}", agent_id=agent_id
            )
        ]
    try:
        return AgentConfig.model_validate(raw or {}), []
    except ValidationError as exc:
        return None, [
            Diagnostic(
                severity="error",
                path=path,
                message=f"invalid agent.yaml: {_validation_message(exc)}",
                agent_id=agent_id,
            )
        ]


def load_bundle_config(
    path: Path, *, bundle_id: str
) -> tuple[BundleConfig | None, list[Diagnostic]]:
    """Parse and validate a ``bundle.yaml`` file.

    Returns ``(config, [])`` on success, or ``(None, [error diagnostic])`` on
    any YAML or validation failure. Never raises.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, [
            Diagnostic(
                severity="error", path=path, message=f"invalid YAML: {exc}", bundle_id=bundle_id
            )
        ]
    try:
        return BundleConfig.model_validate(raw or {}), []
    except ValidationError as exc:
        return None, [
            Diagnostic(
                severity="error",
                path=path,
                message=f"invalid bundle.yaml: {_validation_message(exc)}",
                bundle_id=bundle_id,
            )
        ]


__all__ = [
    "AgentConfig",
    "ApprovalPolicyName",
    "BundleConfig",
    "CompactionConfig",
    "ConnectionConfig",
    "LimitsConfig",
    "ModelConfig",
    "load_agent_config",
    "load_bundle_config",
]
