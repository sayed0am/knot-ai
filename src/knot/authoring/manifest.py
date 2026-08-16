"""The compiled, per-agent manifest: what phase 2 hands to the runtime.

``AgentManifest`` is a plain data snapshot of one agent's fully-resolved
capabilities (own tools, bundle tools, connection-qualified tools, skills,
approvals) plus its identity (id, instructions path and hash) and its
declared subagent ids. It is a ``WireModel`` because it is meant to be
diffed and eventually served, so its JSON shape (camelCase, ``extra="forbid"``)
is held to the same stability bar as the rest of the wire protocol.

Serialization is deterministic: :func:`serialize_manifest` sorts collections
before dumping and sorts JSON object keys on the way out, so two compiles of
an unchanged fleet produce byte-identical files and a real bundle edit shows
up as a small, readable diff.
"""

from __future__ import annotations

import json

from pydantic import Field

from knot.authoring.config import (
    ApprovalPolicyName,
    CompactionConfig,
    LimitsConfig,
    ModelConfig,
    RepeatGuardConfig,
)
from knot.providers.messages import WireModel
from knot.providers.types import JSONValue


class ManifestTool(WireModel):
    """One tool as it appears in a compiled agent's manifest."""

    name: str
    description: str
    input_schema: dict[str, JSONValue]
    source: str
    approval: ApprovalPolicyName = "never"
    idempotent: bool = False
    executable: bool


class ManifestSkill(WireModel):
    """One skill as it appears in a compiled agent's manifest."""

    id: str
    name: str
    description: str
    source: str
    path: str


class AgentManifest(WireModel):
    """The complete, serializable output of compiling one agent."""

    agent_id: str
    instructions_path: str
    instructions_sha256: str
    model: ModelConfig | None = None
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    repeat_guard: RepeatGuardConfig = Field(default_factory=RepeatGuardConfig)
    tools: list[ManifestTool] = Field(default_factory=list)
    skills: list[ManifestSkill] = Field(default_factory=list)
    subagent_ids: list[str] = Field(default_factory=list)
    use: list[str] = Field(default_factory=list)


def serialize_manifest(manifest: AgentManifest) -> str:
    """Render a manifest as deterministic, diff-friendly JSON text.

    Sorts JSON object keys on top of the manifest's own already-sorted
    lists (see ``knot.authoring.compile.compile_agent``), so this is safe
    to compare byte-for-byte across compiles and to write straight to disk.
    """
    data = manifest.model_dump(mode="json", by_alias=True)
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


__all__ = ["AgentManifest", "ManifestSkill", "ManifestTool", "serialize_manifest"]
