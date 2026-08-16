"""Phase-2 compile: discovery -> imported tools -> resolved bundles -> manifest.

Where discovery only classifies the filesystem, this module actually
imports authored tool modules, resolves ``use:`` bundle references,
resolves MCP-style connections against their committed offline snapshots,
merges approval policies, rejects name collisions, and emits one
:class:`AgentManifest` per agent plus the live :class:`AgentTool` objects
the runtime needs to actually execute them.

Nothing here ever makes a network call: connection tools come exclusively
from committed snapshot files (``shared/<bundle>/snapshots/<connection>.json``);
a missing or malformed snapshot is a compile error, not a fetch.

One agent's compile failure never stops the rest of the fleet: every
resolution step collects diagnostics and continues, and only at the end
does an agent (or bundle) with any error diagnostic get excluded from
having a manifest produced for it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from knot.authoring.config import (
    AgentConfig,
    ApprovalPolicyName,
    ConnectionConfig,
    load_agent_config,
    load_bundle_config,
)
from knot.authoring.discovery import (
    AgentDiscovery,
    BundleDiscovery,
    Diagnostic,
    FleetDiscovery,
    SkillEntry,
    discover_fleet,
)
from knot.authoring.manifest import AgentManifest, ManifestSkill, ManifestTool, serialize_manifest
from knot.authoring.skills import Skill, build_load_skill_tool, parse_skill_file
from knot.authoring.tools import compile_tool_module
from knot.core.hitl import build_ask_user_tool
from knot.core.spill_tool import build_read_tool_output_placeholder
from knot.core.tools import AgentTool


def _resolve_approval(
    approvals: dict[str, ApprovalPolicyName], tool_name: str
) -> ApprovalPolicyName:
    """Exact key first, then longest matching ``__``-qualified suffix key.

    Mirrors ``knot.core.hitl.policies._resolve_policy``'s matching rule so a
    bundle's ``approvals: {add: always}`` gates every connection's
    ``<connection>__add`` the same way in what ends up in the manifest (here)
    as in what the runtime decision hook actually enforces — a bundle author
    writes one approval key without needing to know which connection name
    it will end up qualified under.
    """
    if tool_name in approvals:
        return approvals[tool_name]
    best_key: str | None = None
    for key in approvals:
        suffix = f"__{key}"
        if tool_name.endswith(suffix) and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return approvals[best_key] if best_key is not None else "never"


@dataclass(frozen=True, slots=True)
class Capability:
    """A tool paired with where it came from, for collision reporting."""

    tool: AgentTool
    source: str


@dataclass(frozen=True, slots=True)
class CompiledBundle:
    """One successfully-resolved ``shared/<bundle>/``.

    Bundle resolution is atomic: a :class:`CompiledBundle` only exists if
    every one of its tools, skills, and connections resolved cleanly. An
    agent referencing a bundle that failed to compile gets its own error
    (see ``compile_agent``) rather than a partial bundle.
    """

    bundle_id: str
    tools: dict[str, Capability]
    skills: dict[str, Skill]
    approvals: dict[str, ApprovalPolicyName]
    connections: dict[str, ConnectionConfig] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FleetContext:
    """Fleet-wide state a single agent's compile needs: its root and every
    bundle that was discovered (``None`` for one that failed to compile, so
    "unknown bundle" and "bundle exists but is broken" stay distinguishable)."""

    root: Path
    bundles: dict[str, CompiledBundle | None]


@dataclass(frozen=True, slots=True)
class CompiledAgent:
    """The result of compiling one agent: either a manifest, or diagnostics.

    ``subagents`` holds every declared subagent's own full compile result,
    keyed by its (bare) id — a subagent is a full agent root, so this is
    exactly one more ``CompiledAgent`` per level, all the way down through
    nested subagents-of-subagents. ``tools`` deliberately does NOT include a
    live tool for any subagent: a subagent is lowered into a manifest entry
    here (see ``compile_agent``), but its actual delegation tool — the one
    with a real executor — is assembled at runtime (see
    ``knot.authoring.runtime``), never at compile time.
    """

    agent_id: str
    ok: bool
    manifest: AgentManifest | None
    tools: dict[str, AgentTool]
    subagents: dict[str, CompiledAgent] = field(default_factory=dict)
    diagnostics: tuple[Diagnostic, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class CompiledFleet:
    """The result of compiling every agent (and bundle) under a fleet root."""

    root: Path
    agents: dict[str, CompiledAgent]
    bundle_diagnostics: tuple[Diagnostic, ...] = field(default_factory=tuple)
    bundles: dict[str, CompiledBundle | None] = field(default_factory=dict)


def _compile_tool_paths(
    paths: tuple[Path, ...],
    *,
    diagnostics: list[Diagnostic],
    agent_id: str | None = None,
    bundle_id: str | None = None,
) -> dict[str, AgentTool]:
    tools: dict[str, AgentTool] = {}
    origins: dict[str, Path] = {}
    for path in paths:
        try:
            agent_tool = compile_tool_module(path)
        except Exception as exc:  # noqa: BLE001 - import/inspection is an isolation boundary
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    path=path,
                    message=f"failed to compile tool module {path.name!r}: {exc}",
                    agent_id=agent_id,
                    bundle_id=bundle_id,
                )
            )
            continue
        if agent_tool.name in tools:
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    path=path,
                    message=(
                        f"duplicate tool name {agent_tool.name!r}: defined by both "
                        f"{origins[agent_tool.name].name!r} and {path.name!r}"
                    ),
                    agent_id=agent_id,
                    bundle_id=bundle_id,
                )
            )
            continue
        tools[agent_tool.name] = agent_tool
        origins[agent_tool.name] = path
    return tools


def _compile_skills(
    entries: tuple[SkillEntry, ...],
    *,
    source: str,
    diagnostics: list[Diagnostic],
    agent_id: str | None = None,
    bundle_id: str | None = None,
) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for entry in entries:
        skill_md = entry.path / "SKILL.md"
        try:
            skill = parse_skill_file(skill_md, skill_id=entry.skill_id, source=source)
        except Exception as exc:  # noqa: BLE001 - a malformed skill fails only itself
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    path=skill_md,
                    message=str(exc),
                    agent_id=agent_id,
                    bundle_id=bundle_id,
                )
            )
            continue
        skills[skill.skill_id] = skill
    return skills


def _load_snapshot(
    bundle_dir: Path, bundle_id: str, connection: str, diagnostics: list[Diagnostic]
) -> list[dict] | None:
    snapshot_path = bundle_dir / "snapshots" / f"{connection}.json"
    if not snapshot_path.is_file():
        diagnostics.append(
            Diagnostic(
                severity="error",
                path=snapshot_path,
                message=(
                    f"missing committed connection snapshot for {connection!r} "
                    f"(expected {snapshot_path})"
                ),
                bundle_id=bundle_id,
            )
        )
        return None
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        diagnostics.append(
            Diagnostic(
                severity="error",
                path=snapshot_path,
                message=f"invalid connection snapshot: {exc}",
                bundle_id=bundle_id,
            )
        )
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tools"), list):
        diagnostics.append(
            Diagnostic(
                severity="error",
                path=snapshot_path,
                message="invalid connection snapshot shape: expected an object with a 'tools' list",
                bundle_id=bundle_id,
            )
        )
        return None
    return data["tools"]


def _strip_provided_arguments(
    schema: dict,
    provided_arguments: dict[str, str],
    *,
    bundle_id: str,
    connection: str,
    tool_name: str,
    diagnostics: list[Diagnostic],
) -> dict:
    """Remove every ``provided_arguments`` key from a tool's model-facing schema.

    A provided argument is injected at execution time (see
    ``knot.authoring.mcp_client.build_connection_executor``), never authored
    by the model, so it must not appear in what the model is shown. A
    declared key absent from this particular tool's schema is only a
    warning: ``provided_arguments`` is declared once per connection but only
    some of a connection's tools may actually accept that argument.
    """
    if not provided_arguments:
        return schema
    properties = dict(schema.get("properties") or {})
    required = [r for r in (schema.get("required") or [])]
    changed = False
    for key in provided_arguments:
        if key not in properties:
            diagnostics.append(
                Diagnostic(
                    severity="warning",
                    path=Path(f"shared/{bundle_id}/bundle.yaml"),
                    message=(
                        f"connection {connection!r}: provided_arguments key {key!r} "
                        f"was not found in tool {tool_name!r}'s schema"
                    ),
                    bundle_id=bundle_id,
                )
            )
            continue
        properties.pop(key, None)
        if key in required:
            required.remove(key)
        changed = True
    if not changed:
        return schema
    stripped = dict(schema)
    stripped["properties"] = properties
    if required:
        stripped["required"] = required
    else:
        stripped.pop("required", None)
    return stripped


def _connection_tools(
    bundle_id: str,
    connection: str,
    config: ConnectionConfig,
    snapshot_tools: list[dict],
    diagnostics: list[Diagnostic],
) -> dict[str, AgentTool]:
    """Resolve one connection's snapshot into execute-less, qualified tools.

    Connection tools compile execute-less (``execute_fn=None``): a runtime
    executor for them is wired in by ``knot.authoring.runtime`` at harness
    assembly, never here — compile time only ever reads the committed
    snapshot, never the network.
    """
    available = {
        t["name"]: t
        for t in snapshot_tools
        if isinstance(t, dict) and isinstance(t.get("name"), str)
    }
    if config.allow is None:
        selected = sorted(available)
    else:
        selected = []
        for name in config.allow:
            if name not in available:
                diagnostics.append(
                    Diagnostic(
                        severity="warning",
                        path=Path(f"shared/{bundle_id}/bundle.yaml"),
                        message=(
                            f"connection {connection!r}: allow-listed tool {name!r} "
                            "was not found in its snapshot"
                        ),
                        bundle_id=bundle_id,
                    )
                )
                continue
            selected.append(name)

    tools: dict[str, AgentTool] = {}
    for name in selected:
        spec = available[name]
        qualified = f"{connection}__{name}"
        schema = dict(spec.get("inputSchema") or {})
        schema = _strip_provided_arguments(
            schema,
            config.provided_arguments,
            bundle_id=bundle_id,
            connection=connection,
            tool_name=name,
            diagnostics=diagnostics,
        )
        tools[qualified] = AgentTool(
            name=qualified,
            description=str(spec.get("description") or ""),
            parameters=schema,
            execute_fn=None,
            idempotent=False,
        )
    return tools


def _compile_bundle(discovery: BundleDiscovery) -> tuple[CompiledBundle | None, list[Diagnostic]]:
    diagnostics: list[Diagnostic] = list(discovery.diagnostics)
    if discovery.has_errors or discovery.config_path is None:
        return None, diagnostics

    config, config_diagnostics = load_bundle_config(
        discovery.config_path, bundle_id=discovery.bundle_id
    )
    diagnostics.extend(config_diagnostics)
    if config is None:
        return None, diagnostics

    source = f"bundle:{discovery.bundle_id}"
    tools: dict[str, Capability] = {
        name: Capability(tool=t, source=source)
        for name, t in _compile_tool_paths(
            discovery.tool_paths, diagnostics=diagnostics, bundle_id=discovery.bundle_id
        ).items()
    }
    skills = _compile_skills(
        discovery.skills, source=source, diagnostics=diagnostics, bundle_id=discovery.bundle_id
    )

    for connection, connection_config in config.connections.items():
        snapshot_tools = _load_snapshot(
            discovery.path, discovery.bundle_id, connection, diagnostics
        )
        if snapshot_tools is None:
            continue
        connection_source = f"connection:{discovery.bundle_id}/{connection}"
        resolved = _connection_tools(
            discovery.bundle_id, connection, connection_config, snapshot_tools, diagnostics
        )
        for name, agent_tool in resolved.items():
            if name in tools:
                diagnostics.append(
                    Diagnostic(
                        severity="error",
                        path=discovery.path,
                        message=(
                            f"tool name collision on {name!r}: defined by both "
                            f"{tools[name].source!r} and {connection_source!r}"
                        ),
                        bundle_id=discovery.bundle_id,
                    )
                )
                continue
            tools[name] = Capability(tool=agent_tool, source=connection_source)

    if any(d.severity == "error" for d in diagnostics):
        return None, diagnostics

    return (
        CompiledBundle(
            bundle_id=discovery.bundle_id,
            tools=tools,
            skills=skills,
            approvals=dict(config.approvals),
            connections=dict(config.connections),
        ),
        diagnostics,
    )


#: A delegation call's schema is fixed and uniform across every subagent: a
#: single free-text ``message`` to pack the whole request into (see
#: ``knot.authoring.runtime`` for how it's actually executed).
_SUBAGENT_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "description": "The message to delegate to this subagent.",
        }
    },
    "required": ["message"],
    "additionalProperties": False,
}


def _subagent_description(sub_discovery: AgentDiscovery) -> str | None:
    """A subagent's own ``description``, or ``None`` if it has none.

    Load failures here (missing/invalid ``agent.yaml``) are intentionally
    swallowed: the subsequent recursive ``compile_agent`` call reports those
    more informatively on its own, with the subagent's full diagnostics
    attached to the parent (see the subagent-lowering block below); this
    helper only needs a yes/no answer for the description requirement.
    """
    if sub_discovery.config_path is None:
        return None
    config, _diagnostics = load_agent_config(
        sub_discovery.config_path, agent_id=sub_discovery.agent_id
    )
    if config is None:
        return None
    return config.description


def _failed_agent(agent_id: str, diagnostics: list[Diagnostic]) -> CompiledAgent:
    return CompiledAgent(
        agent_id=agent_id, ok=False, manifest=None, tools={}, diagnostics=tuple(diagnostics)
    )


def compile_agent(discovery: AgentDiscovery, fleet: FleetContext) -> CompiledAgent:
    """Compile one agent: resolve its bundles, tools, skills, and approvals."""
    diagnostics: list[Diagnostic] = list(discovery.diagnostics)
    if discovery.has_errors:
        return _failed_agent(discovery.agent_id, diagnostics)

    assert discovery.instructions_path is not None  # guaranteed by has_errors above

    config = None
    if discovery.config_path is not None:
        config, config_diagnostics = load_agent_config(
            discovery.config_path, agent_id=discovery.agent_id
        )
        diagnostics.extend(config_diagnostics)
        if config is None:
            return _failed_agent(discovery.agent_id, diagnostics)

    if config is None:
        config = AgentConfig()

    capabilities: dict[str, Capability] = {}
    skills: dict[str, Skill] = {}
    approvals: dict[str, ApprovalPolicyName] = {}

    def merge_capabilities(new: dict[str, Capability]) -> None:
        for name, capability in new.items():
            existing = capabilities.get(name)
            if existing is not None:
                diagnostics.append(
                    Diagnostic(
                        severity="error",
                        path=discovery.path,
                        message=(
                            f"tool name collision on {name!r}: defined by both "
                            f"{existing.source!r} and {capability.source!r}"
                        ),
                        agent_id=discovery.agent_id,
                    )
                )
                continue
            capabilities[name] = capability

    def merge_skills(new: dict[str, Skill]) -> None:
        for skill_id, skill in new.items():
            existing = skills.get(skill_id)
            if existing is not None:
                diagnostics.append(
                    Diagnostic(
                        severity="error",
                        path=discovery.path,
                        message=(
                            f"skill id collision on {skill_id!r}: defined by both "
                            f"{existing.source!r} and {skill.source!r}"
                        ),
                        agent_id=discovery.agent_id,
                    )
                )
                continue
            skills[skill_id] = skill

    for bundle_ref in config.use:
        if bundle_ref not in fleet.bundles:
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    path=discovery.path,
                    message=f"unknown bundle reference {bundle_ref!r} in use:",
                    agent_id=discovery.agent_id,
                )
            )
            continue
        bundle = fleet.bundles[bundle_ref]
        if bundle is None:
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    path=discovery.path,
                    message=f"bundle {bundle_ref!r} failed to compile; see its own diagnostics",
                    agent_id=discovery.agent_id,
                )
            )
            continue
        merge_capabilities(bundle.tools)
        merge_skills(bundle.skills)
        approvals.update(bundle.approvals)

    own_tools = _compile_tool_paths(
        discovery.tool_paths, diagnostics=diagnostics, agent_id=discovery.agent_id
    )
    merge_capabilities(
        {name: Capability(tool=t, source="authored") for name, t in own_tools.items()}
    )

    own_skills = _compile_skills(
        discovery.skills, source="authored", diagnostics=diagnostics, agent_id=discovery.agent_id
    )
    merge_skills(own_skills)

    approvals.update(config.approvals)  # agent-level approvals override bundle-level ones

    for tool_name in approvals:
        # A bare key matches either an exact capability name or, via the
        # same "__"-suffix rule _resolve_approval applies, at least one
        # qualified capability (e.g. a connection-tool bundle approval
        # written as "add" matching "crm__add") — only warn when neither
        # ever matched anything, since that's the actual typo signal.
        matches_something = tool_name in capabilities or any(
            name.endswith(f"__{tool_name}") for name in capabilities
        )
        if not matches_something:
            diagnostics.append(
                Diagnostic(
                    severity="warning",
                    path=discovery.path,
                    message=f"approval policy set for unknown tool {tool_name!r}",
                    agent_id=discovery.agent_id,
                )
            )

    if any(d.severity == "error" for d in diagnostics):
        return _failed_agent(discovery.agent_id, diagnostics)

    # INSERTION POINT for framework-floor tools: ask_user is added
    # unconditionally (the framework floor: every agent gets it); load_skill
    # is added here iff the agent has skills. Both are subject to the same
    # collision rule as every other capability.
    ask_user_tool = build_ask_user_tool()
    if ask_user_tool.name in capabilities:
        existing = capabilities[ask_user_tool.name]
        diagnostics.append(
            Diagnostic(
                severity="error",
                path=discovery.path,
                message=(
                    f"tool name collision on {ask_user_tool.name!r}: defined by both "
                    f"{existing.source!r} and 'builtin' (framework-floor tool)"
                ),
                agent_id=discovery.agent_id,
            )
        )
    else:
        capabilities[ask_user_tool.name] = Capability(tool=ask_user_tool, source="builtin")

    # read_tool_output is unconditional too (every spill needs it retrievable
    # regardless of what tools an agent happens to have), but it compiles
    # execute-less: it needs (store, session_id), which only exist at
    # runtime assembly (see knot.authoring.runtime._build_harness).
    read_tool_output_tool = build_read_tool_output_placeholder()
    if read_tool_output_tool.name in capabilities:
        existing = capabilities[read_tool_output_tool.name]
        diagnostics.append(
            Diagnostic(
                severity="error",
                path=discovery.path,
                message=(
                    f"tool name collision on {read_tool_output_tool.name!r}: defined by both "
                    f"{existing.source!r} and 'builtin' (framework-floor tool)"
                ),
                agent_id=discovery.agent_id,
            )
        )
    else:
        capabilities[read_tool_output_tool.name] = Capability(
            tool=read_tool_output_tool, source="builtin"
        )

    if skills:
        load_skill_tool = build_load_skill_tool(list(skills.values()))
        capabilities[load_skill_tool.name] = Capability(tool=load_skill_tool, source="builtin")

    if any(d.severity == "error" for d in diagnostics):
        return _failed_agent(discovery.agent_id, diagnostics)

    # Subagent lowering: each discovered subagent is a full agent root,
    # recursively compiled against this same fleet context (its own tools,
    # skills, and bundle references resolve exactly like a top-level
    # agent's). A subagent missing a required description, or one whose own
    # compile fails, fails the parent's compile too — the child's own
    # diagnostics are attached, prefixed with which subagent they came from,
    # rather than the parent getting one opaque failure. A subagent that
    # lowers successfully is folded into `capabilities` as an execute-less
    # capability (subject to the exact same collision rule as everything
    # else) so its manifest entry sorts and collision-checks uniformly with
    # every other tool; its full `CompiledAgent` is kept on `subagents` for
    # the runtime package to actually execute delegation calls with.
    subagents: dict[str, CompiledAgent] = {}
    for sub_discovery in discovery.subagents:
        description = _subagent_description(sub_discovery)
        if not description:
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    path=sub_discovery.path,
                    message=(
                        f"subagent {sub_discovery.agent_id!r} is missing a required "
                        "'description' in its agent.yaml"
                    ),
                    agent_id=discovery.agent_id,
                )
            )

        child_compiled = compile_agent(sub_discovery, fleet)
        if not child_compiled.ok:
            for child_diagnostic in child_compiled.diagnostics:
                diagnostics.append(
                    Diagnostic(
                        severity=child_diagnostic.severity,
                        path=child_diagnostic.path,
                        message=(
                            f"subagent {sub_discovery.agent_id!r}: {child_diagnostic.message}"
                        ),
                        agent_id=discovery.agent_id,
                    )
                )
            continue

        subagents[sub_discovery.agent_id] = child_compiled
        if not description:
            continue

        delegation_tool = AgentTool(
            name=sub_discovery.agent_id,
            description=description,
            parameters=_SUBAGENT_TOOL_SCHEMA,
            execute_fn=None,
        )
        merge_capabilities(
            {sub_discovery.agent_id: Capability(tool=delegation_tool, source="subagent")}
        )

    if any(d.severity == "error" for d in diagnostics):
        return _failed_agent(discovery.agent_id, diagnostics)

    instructions_sha256 = hashlib.sha256(discovery.instructions_path.read_bytes()).hexdigest()

    # A subagent's manifest entry is always `executable: true` even though
    # its `Capability.tool.execute_fn` is `None` here: it genuinely is
    # executable, just not by this compile step — the runtime package
    # assembles its real delegation executor (see `CompiledAgent.subagents`).
    manifest_tools = sorted(
        (
            ManifestTool(
                name=capability.tool.name,
                description=capability.tool.description,
                input_schema=dict(capability.tool.parameters),
                source=capability.source,
                approval=_resolve_approval(approvals, capability.tool.name),
                idempotent=capability.tool.idempotent,
                executable=(
                    True
                    if capability.source == "subagent"
                    else capability.tool.execute_fn is not None
                ),
            )
            for capability in capabilities.values()
        ),
        key=lambda t: t.name,
    )
    manifest_skills = sorted(
        (
            ManifestSkill(
                id=skill.skill_id,
                name=skill.name,
                description=skill.description,
                source=skill.source,
                path=str(skill.path.relative_to(fleet.root)),
            )
            for skill in skills.values()
        ),
        key=lambda s: s.id,
    )
    subagent_ids = sorted(subagents)

    manifest = AgentManifest(
        agent_id=discovery.agent_id,
        instructions_path=str(discovery.instructions_path.relative_to(fleet.root)),
        instructions_sha256=instructions_sha256,
        model=config.model,
        limits=config.limits,
        compaction=config.compaction,
        repeat_guard=config.repeat_guard,
        tools=manifest_tools,
        skills=manifest_skills,
        subagent_ids=subagent_ids,
        use=sorted(config.use),
    )

    # Subagents never get a live tool here (see `CompiledAgent` docstring):
    # an execute-less `AgentTool` would wrongly park the run as a question
    # instead of actually delegating.
    live_tools = {
        capability.tool.name: capability.tool
        for capability in capabilities.values()
        if capability.source != "subagent"
    }

    return CompiledAgent(
        agent_id=discovery.agent_id,
        ok=True,
        manifest=manifest,
        tools=live_tools,
        subagents=subagents,
        diagnostics=tuple(diagnostics),
    )


def compile_fleet(root: Path) -> CompiledFleet:
    """Discover and compile every agent and bundle under ``root``."""
    discovery: FleetDiscovery = discover_fleet(root)

    bundle_map: dict[str, CompiledBundle | None] = {}
    bundle_diagnostics: list[Diagnostic] = []
    for bundle_discovery in discovery.bundles:
        compiled_bundle, diagnostics = _compile_bundle(bundle_discovery)
        bundle_diagnostics.extend(diagnostics)
        bundle_map[bundle_discovery.bundle_id] = compiled_bundle

    fleet_context = FleetContext(root=root, bundles=bundle_map)
    agents = {
        agent_discovery.agent_id: compile_agent(agent_discovery, fleet_context)
        for agent_discovery in discovery.agents
    }

    return CompiledFleet(
        root=root,
        agents=agents,
        bundle_diagnostics=tuple(bundle_diagnostics),
        bundles=bundle_map,
    )


def write_manifests(fleet: CompiledFleet, out_dir: Path) -> None:
    """Write every successfully-compiled agent's manifest to ``<out_dir>/<agent_id>.json``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for agent_id, compiled in fleet.agents.items():
        if compiled.manifest is None:
            continue
        path = out_dir / f"{agent_id}.json"
        path.write_text(serialize_manifest(compiled.manifest), encoding="utf-8")


def diff_manifests(fleet: CompiledFleet, out_dir: Path) -> list[str]:
    """Return the ids of agents whose manifest would change if written now.

    Covers a missing committed file, changed content, and a committed file
    for an agent that no longer exists in the fleet at all — the
    bundle-edit blast-radius list a bundle author needs before touching a
    shared tool.
    """
    changed: set[str] = set()
    for agent_id, compiled in fleet.agents.items():
        if compiled.manifest is None:
            continue
        path = out_dir / f"{agent_id}.json"
        fresh = serialize_manifest(compiled.manifest)
        if not path.is_file() or path.read_text(encoding="utf-8") != fresh:
            changed.add(agent_id)

    if out_dir.is_dir():
        known_ids = set(fleet.agents)
        for path in out_dir.glob("*.json"):
            if path.stem not in known_ids:
                changed.add(path.stem)

    return sorted(changed)


__all__ = [
    "Capability",
    "CompiledAgent",
    "CompiledBundle",
    "CompiledFleet",
    "FleetContext",
    "compile_agent",
    "compile_fleet",
    "diff_manifests",
    "write_manifests",
]
