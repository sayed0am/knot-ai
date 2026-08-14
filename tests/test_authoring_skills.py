"""SKILL.md parsing and the load_skill framework-floor tool."""

from __future__ import annotations

from pathlib import Path

import pytest
from authoring_fixtures import write_files

from knot.authoring.skills import (
    Skill,
    SkillParseError,
    build_load_skill_tool,
    parse_skill_file,
    render_skill_listing,
)
from knot.core.tools import execute_tool
from knot.providers.messages import ToolCall


def test_parses_frontmatter_and_body(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "onboarding/SKILL.md": (
                "---\n"
                "name: Onboarding\n"
                "description: Helps a new user get set up.\n"
                "---\n"
                "# Onboarding\n\nDo the first thing, then the second thing.\n"
            )
        },
    )

    skill = parse_skill_file(
        tmp_path / "onboarding" / "SKILL.md", skill_id="onboarding", source="authored"
    )

    assert skill.skill_id == "onboarding"
    assert skill.name == "Onboarding"
    assert skill.description == "Helps a new user get set up."
    assert skill.body == "# Onboarding\n\nDo the first thing, then the second thing."
    assert skill.source == "authored"


def test_extra_frontmatter_keys_are_tolerated(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "s/SKILL.md": (
                "---\n"
                "name: S\n"
                "description: D\n"
                "some_other_tool_key: whatever\n"
                "license: MIT\n"
                "---\n"
                "body\n"
            )
        },
    )

    skill = parse_skill_file(tmp_path / "s" / "SKILL.md", skill_id="s", source="authored")

    assert skill.name == "S"
    assert skill.body == "body"


def test_missing_frontmatter_block_raises(tmp_path: Path) -> None:
    write_files(tmp_path, {"s/SKILL.md": "just plain markdown, no frontmatter\n"})

    with pytest.raises(SkillParseError, match="frontmatter"):
        parse_skill_file(tmp_path / "s" / "SKILL.md", skill_id="s", source="authored")


def test_missing_required_name_raises(tmp_path: Path) -> None:
    write_files(tmp_path, {"s/SKILL.md": "---\ndescription: only description\n---\nbody\n"})

    with pytest.raises(SkillParseError, match="name"):
        parse_skill_file(tmp_path / "s" / "SKILL.md", skill_id="s", source="authored")


def test_missing_required_description_raises(tmp_path: Path) -> None:
    write_files(tmp_path, {"s/SKILL.md": "---\nname: only name\n---\nbody\n"})

    with pytest.raises(SkillParseError, match="description"):
        parse_skill_file(tmp_path / "s" / "SKILL.md", skill_id="s", source="authored")


def _skill(skill_id: str, name: str, description: str, body: str, path: Path) -> Skill:
    return Skill(
        skill_id=skill_id,
        name=name,
        description=description,
        body=body,
        path=path,
        source="authored",
    )


async def test_load_skill_returns_the_bodies_of_available_skills(tmp_path: Path) -> None:
    skills = [
        _skill("a", "A", "does a", "a body", tmp_path / "a"),
        _skill("b", "B", "does b", "b body", tmp_path / "b"),
    ]
    load_skill = build_load_skill_tool(skills)

    call = ToolCall(id="c1", name="load_skill", arguments={"skill": "b"})
    result, is_error = await execute_tool(load_skill, call)

    assert is_error is False
    assert result.text == "b body"


async def test_load_skill_unknown_id_is_an_error_result_listing_available_ids(
    tmp_path: Path,
) -> None:
    skills = [_skill("a", "A", "does a", "a body", tmp_path / "a")]
    load_skill = build_load_skill_tool(skills)

    call = ToolCall(id="c1", name="load_skill", arguments={"skill": "nonexistent"})
    result, is_error = await execute_tool(load_skill, call)

    assert is_error is True
    assert "nonexistent" in result.text
    assert "a" in result.text


def test_render_skill_listing_includes_every_skill(tmp_path: Path) -> None:
    skills = [
        _skill("a", "Alpha", "the first skill", "body-a", tmp_path / "a"),
        _skill("b", "Beta", "the second skill", "body-b", tmp_path / "b"),
    ]

    listing = render_skill_listing(skills)

    assert "Alpha" in listing
    assert "the first skill" in listing
    assert "Beta" in listing
    assert "the second skill" in listing
    assert "load_skill" in listing


def test_render_skill_listing_empty_for_no_skills() -> None:
    assert render_skill_listing([]) == ""
