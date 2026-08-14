"""``SKILL.md`` parsing and the framework-provided ``load_skill`` tool.

A skill file is markdown with a YAML frontmatter block (delimited by ``---``
lines) that must declare ``name`` and ``description``; everything after the
closing delimiter is the skill's body, handed back verbatim by
``load_skill``. Extra frontmatter keys are tolerated so files written for
other purposes still load here unmodified.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from knot.core.tools import AgentTool, AgentToolResult
from knot.providers.messages import TextContent

_FRONTMATTER_DELIMITER = "---"

LOAD_SKILL_TOOL_NAME = "load_skill"


class SkillParseError(Exception):
    """A ``SKILL.md`` file is missing its frontmatter or a required field."""


@dataclass(frozen=True, slots=True)
class Skill:
    """One parsed skill, ready to be listed and loaded on demand."""

    skill_id: str
    name: str
    description: str
    body: str
    path: Path
    source: str


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return None, text
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_DELIMITER:
            frontmatter = "".join(lines[1:index])
            body = "".join(lines[index + 1 :])
            return frontmatter, body
    return None, text


def parse_skill_file(path: Path, *, skill_id: str, source: str) -> Skill:
    """Parse one ``SKILL.md`` file into a :class:`Skill`.

    Raises :class:`SkillParseError` if the frontmatter block is missing, is
    not a YAML mapping, or lacks ``name``/``description``.
    """
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text)
    if frontmatter is None:
        raise SkillParseError(f"skill {skill_id!r}: SKILL.md has no YAML frontmatter block")

    try:
        data = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise SkillParseError(f"skill {skill_id!r}: invalid frontmatter YAML: {exc}") from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SkillParseError(f"skill {skill_id!r}: frontmatter must be a YAML mapping")

    name = data.get("name")
    if not name:
        raise SkillParseError(f"skill {skill_id!r}: frontmatter is missing required field 'name'")
    description = data.get("description")
    if not description:
        raise SkillParseError(
            f"skill {skill_id!r}: frontmatter is missing required field 'description'"
        )

    return Skill(
        skill_id=skill_id,
        name=str(name),
        description=str(description),
        body=body.strip("\n"),
        path=path.parent,
        source=source,
    )


def build_load_skill_tool(skills: Sequence[Skill]) -> AgentTool:
    """Build the framework-floor ``load_skill`` tool for a set of skills.

    Present in an agent's manifest iff the agent has at least one skill (see
    ``knot.authoring.compile``). Requesting an unknown skill id raises,
    which ``knot.core.tools.execute_tool`` turns into an error result
    listing the available ids — never an unhandled exception.
    """
    by_id = {skill.skill_id: skill for skill in skills}
    schema = {
        "type": "object",
        "properties": {"skill": {"type": "string", "enum": sorted(by_id)}},
        "required": ["skill"],
        "additionalProperties": False,
    }

    async def execute_fn(
        tool_call_id: str,
        arguments: object,
        signal: object = None,
        on_update: object = None,
    ) -> AgentToolResult:
        requested = arguments.get("skill") if isinstance(arguments, dict) else None
        skill = by_id.get(requested) if isinstance(requested, str) else None
        if skill is None:
            available = ", ".join(sorted(by_id)) or "(none)"
            raise ValueError(f"unknown skill {requested!r}; available skills: {available}")
        return AgentToolResult(content=[TextContent(text=skill.body)])

    return AgentTool(
        name=LOAD_SKILL_TOOL_NAME,
        description="Load the full instructions for one of this agent's available skills by id.",
        parameters=schema,
        execute_fn=execute_fn,
        idempotent=True,
    )


def render_skill_listing(skills: Sequence[Skill]) -> str:
    """Render a system-prompt block listing each skill's name and description."""
    if not skills:
        return ""
    lines = ["Available skills (call load_skill with the id to load full instructions):"]
    for skill in sorted(skills, key=lambda s: s.skill_id):
        lines.append(f"- {skill.skill_id}: {skill.name} - {skill.description}")
    return "\n".join(lines)


__all__ = [
    "LOAD_SKILL_TOOL_NAME",
    "Skill",
    "SkillParseError",
    "build_load_skill_tool",
    "parse_skill_file",
    "render_skill_listing",
]
