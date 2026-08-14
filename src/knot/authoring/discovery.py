"""Phase-1 fleet discovery.

Walks a fleet's directory tree and classifies what it finds into agents,
bundles, tools, skills, and subagents. This module never imports authored
Python: tool modules are only ever *listed* here, never executed. That work
belongs to phase 2 (:mod:`knot.authoring.compile`), which can therefore fail
one agent's tools without discovery itself being at risk.

An agent (or bundle, or skill, or subagent) is simply a directory; its id is
its directory name. Unknown files at an agent/bundle root are assumed to be
author notes and are ignored silently; unknown *directories* are reported as
a warning and never descended into.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

#: Shared identifier grammar for agent ids, bundle ids, skill ids, subagent
#: ids, and (see ``knot.authoring.tools``) tool names.
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")

Severity = Literal["error", "warning"]

_RECOGNIZED_AGENT_DIRS = {"tools", "skills", "subagents"}
_RECOGNIZED_BUNDLE_DIRS = {"tools", "skills", "snapshots"}


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One discovery/compile finding, attributable to an agent and/or bundle."""

    severity: Severity
    path: Path
    message: str
    agent_id: str | None = None
    bundle_id: str | None = None

    def __str__(self) -> str:
        return f"[{self.severity}] {self.path}: {self.message}"


@dataclass(frozen=True, slots=True)
class SkillEntry:
    """A ``skills/<id>/`` directory that has a ``SKILL.md`` file."""

    skill_id: str
    path: Path


@dataclass(frozen=True, slots=True)
class AgentDiscovery:
    """The classified contents of one agent (or subagent) directory."""

    agent_id: str
    path: Path
    instructions_path: Path | None
    config_path: Path | None
    tool_paths: tuple[Path, ...]
    skills: tuple[SkillEntry, ...]
    subagents: tuple[AgentDiscovery, ...]
    diagnostics: tuple[Diagnostic, ...] = field(default_factory=tuple)

    @property
    def has_errors(self) -> bool:
        """Whether *this directory's own* diagnostics block it from compiling.

        Subagent diagnostics are excluded: subagents are not lowered/compiled
        by this package (see the framework-floor insertion point in
        ``compile.py``), so a broken subagent does not, by itself, fail its
        parent.
        """
        return any(d.severity == "error" for d in self.diagnostics)


@dataclass(frozen=True, slots=True)
class BundleDiscovery:
    """The classified contents of one ``shared/<bundle>/`` directory."""

    bundle_id: str
    path: Path
    config_path: Path | None
    tool_paths: tuple[Path, ...]
    skills: tuple[SkillEntry, ...]
    diagnostics: tuple[Diagnostic, ...] = field(default_factory=tuple)

    @property
    def has_errors(self) -> bool:
        return any(d.severity == "error" for d in self.diagnostics)


@dataclass(frozen=True, slots=True)
class FleetDiscovery:
    """Every agent and bundle discovered under a fleet root."""

    root: Path
    agents: tuple[AgentDiscovery, ...]
    bundles: tuple[BundleDiscovery, ...]


def _list_tool_files(tools_dir: Path) -> tuple[Path, ...]:
    """Return ``tools/*.py``, ignoring ``_*.py`` files and ``__pycache__``."""
    if not tools_dir.is_dir():
        return ()
    files = [
        entry
        for entry in sorted(tools_dir.iterdir())
        if entry.is_file() and entry.suffix == ".py" and not entry.name.startswith("_")
    ]
    return tuple(files)


def _classify_skills(
    skills_dir: Path, *, agent_id: str | None, bundle_id: str | None
) -> tuple[tuple[SkillEntry, ...], tuple[Diagnostic, ...]]:
    if not skills_dir.is_dir():
        return (), ()

    entries: list[SkillEntry] = []
    diagnostics: list[Diagnostic] = []
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        skill_id = entry.name
        if not ID_PATTERN.match(skill_id):
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    path=entry,
                    message=f"invalid skill id {skill_id!r}: must match {ID_PATTERN.pattern!r}",
                    agent_id=agent_id,
                    bundle_id=bundle_id,
                )
            )
            continue
        if not (entry / "SKILL.md").is_file():
            diagnostics.append(
                Diagnostic(
                    severity="warning",
                    path=entry,
                    message=f"skill directory {skill_id!r} is missing SKILL.md",
                    agent_id=agent_id,
                    bundle_id=bundle_id,
                )
            )
            continue
        entries.append(SkillEntry(skill_id=skill_id, path=entry))
    return tuple(entries), tuple(diagnostics)


def _unknown_dir_diagnostics(
    root_dir: Path,
    recognized: set[str],
    *,
    agent_id: str | None,
    bundle_id: str | None,
    kind: str,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for entry in sorted(root_dir.iterdir()):
        if entry.is_dir() and entry.name not in recognized:
            diagnostics.append(
                Diagnostic(
                    severity="warning",
                    path=entry,
                    message=f"unrecognized directory {entry.name!r} in {kind} root (ignored)",
                    agent_id=agent_id,
                    bundle_id=bundle_id,
                )
            )
        # Unrecognized files are author notes; ignored silently.
    return diagnostics


def discover_agent(agent_dir: Path) -> AgentDiscovery:
    """Classify one agent (or subagent) directory without importing anything."""
    agent_id = agent_dir.name
    diagnostics: list[Diagnostic] = []

    if not ID_PATTERN.match(agent_id):
        diagnostics.append(
            Diagnostic(
                severity="error",
                path=agent_dir,
                message=f"invalid agent id {agent_id!r}: must match {ID_PATTERN.pattern!r}",
                agent_id=agent_id,
            )
        )

    instructions_path: Path | None = agent_dir / "instructions.md"
    if not instructions_path.is_file():
        diagnostics.append(
            Diagnostic(
                severity="error",
                path=agent_dir,
                message="missing required instructions.md",
                agent_id=agent_id,
            )
        )
        instructions_path = None

    config_path: Path | None = agent_dir / "agent.yaml"
    if not config_path.is_file():
        config_path = None

    tool_paths = _list_tool_files(agent_dir / "tools")

    skills, skill_diagnostics = _classify_skills(
        agent_dir / "skills", agent_id=agent_id, bundle_id=None
    )
    diagnostics.extend(skill_diagnostics)

    subagents: list[AgentDiscovery] = []
    subagents_dir = agent_dir / "subagents"
    if subagents_dir.is_dir():
        for entry in sorted(subagents_dir.iterdir()):
            if entry.is_dir():
                subagents.append(discover_agent(entry))

    diagnostics.extend(
        _unknown_dir_diagnostics(
            agent_dir, _RECOGNIZED_AGENT_DIRS, agent_id=agent_id, bundle_id=None, kind="agent"
        )
    )

    return AgentDiscovery(
        agent_id=agent_id,
        path=agent_dir,
        instructions_path=instructions_path,
        config_path=config_path,
        tool_paths=tool_paths,
        skills=skills,
        subagents=tuple(subagents),
        diagnostics=tuple(diagnostics),
    )


def discover_bundle(bundle_dir: Path) -> BundleDiscovery:
    """Classify one ``shared/<bundle>/`` directory without importing anything."""
    bundle_id = bundle_dir.name
    diagnostics: list[Diagnostic] = []

    if not ID_PATTERN.match(bundle_id):
        diagnostics.append(
            Diagnostic(
                severity="error",
                path=bundle_dir,
                message=f"invalid bundle id {bundle_id!r}: must match {ID_PATTERN.pattern!r}",
                bundle_id=bundle_id,
            )
        )

    config_path: Path | None = bundle_dir / "bundle.yaml"
    if not config_path.is_file():
        diagnostics.append(
            Diagnostic(
                severity="error",
                path=bundle_dir,
                message="missing required bundle.yaml",
                bundle_id=bundle_id,
            )
        )
        config_path = None

    tool_paths = _list_tool_files(bundle_dir / "tools")

    skills, skill_diagnostics = _classify_skills(
        bundle_dir / "skills", agent_id=None, bundle_id=bundle_id
    )
    diagnostics.extend(skill_diagnostics)

    diagnostics.extend(
        _unknown_dir_diagnostics(
            bundle_dir, _RECOGNIZED_BUNDLE_DIRS, agent_id=None, bundle_id=bundle_id, kind="bundle"
        )
    )

    return BundleDiscovery(
        bundle_id=bundle_id,
        path=bundle_dir,
        config_path=config_path,
        tool_paths=tool_paths,
        skills=skills,
        diagnostics=tuple(diagnostics),
    )


def discover_fleet(root: Path) -> FleetDiscovery:
    """Discover every agent under ``<root>/agents`` and bundle under ``<root>/shared``."""
    agents_dir = root / "agents"
    bundles_dir = root / "shared"

    agents = (
        tuple(discover_agent(entry) for entry in sorted(agents_dir.iterdir()) if entry.is_dir())
        if agents_dir.is_dir()
        else ()
    )
    bundles = (
        tuple(discover_bundle(entry) for entry in sorted(bundles_dir.iterdir()) if entry.is_dir())
        if bundles_dir.is_dir()
        else ()
    )

    return FleetDiscovery(root=root, agents=agents, bundles=bundles)


__all__ = [
    "ID_PATTERN",
    "AgentDiscovery",
    "BundleDiscovery",
    "Diagnostic",
    "FleetDiscovery",
    "Severity",
    "SkillEntry",
    "discover_agent",
    "discover_bundle",
    "discover_fleet",
]
