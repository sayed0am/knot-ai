"""Phase-1 discovery: classification only, never an import."""

from __future__ import annotations

from pathlib import Path

from authoring_fixtures import write_files

from knot.authoring.discovery import discover_agent, discover_bundle, discover_fleet


def test_minimal_agent_with_only_instructions_is_valid(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/helper/instructions.md": "You are helpful.\n"})

    discovery = discover_agent(tmp_path / "agents" / "helper")

    assert discovery.agent_id == "helper"
    assert discovery.instructions_path == tmp_path / "agents" / "helper" / "instructions.md"
    assert not discovery.has_errors
    assert discovery.diagnostics == ()


def test_missing_instructions_is_an_error_and_excludes_the_agent(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/helper/tools/.keep": ""})
    (tmp_path / "agents" / "helper" / "tools" / ".keep").unlink()

    discovery = discover_agent(tmp_path / "agents" / "helper")

    assert discovery.instructions_path is None
    assert discovery.has_errors
    assert any(
        d.severity == "error" and "instructions.md" in d.message for d in discovery.diagnostics
    )


def test_unknown_directory_warns_and_is_not_descended_into(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "You are helpful.\n",
            "agents/helper/scratch/tools/get_thing.py": (
                "raise RuntimeError('should never be seen')\n"
            ),
        },
    )

    discovery = discover_agent(tmp_path / "agents" / "helper")

    warnings = [d for d in discovery.diagnostics if d.severity == "warning"]
    assert len(warnings) == 1
    assert "scratch" in warnings[0].message
    # Nothing under the unrecognized directory was classified as a tool.
    assert discovery.tool_paths == ()
    assert not discovery.has_errors


def test_unknown_files_at_agent_root_are_ignored_silently(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "You are helpful.\n",
            "agents/helper/NOTES.txt": "author scratch notes\n",
        },
    )

    discovery = discover_agent(tmp_path / "agents" / "helper")

    assert discovery.diagnostics == ()


def test_broken_tool_module_does_not_affect_discovery(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "You are helpful.\n",
            "agents/helper/tools/broken.py": """
                raise RuntimeError("boom at import time")
            """,
        },
    )

    discovery = discover_agent(tmp_path / "agents" / "helper")

    assert discovery.tool_paths == (tmp_path / "agents" / "helper" / "tools" / "broken.py",)
    assert not discovery.has_errors
    assert discovery.diagnostics == ()


def test_tools_ignores_underscore_prefixed_files_and_pycache(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "You are helpful.\n",
            "agents/helper/tools/real_tool.py": "x = 1\n",
            "agents/helper/tools/_helpers.py": "x = 1\n",
            "agents/helper/tools/__pycache__/real_tool.cpython-312.pyc": "not real bytecode",
        },
    )

    discovery = discover_agent(tmp_path / "agents" / "helper")

    assert discovery.tool_paths == (tmp_path / "agents" / "helper" / "tools" / "real_tool.py",)


def test_invalid_agent_id_is_an_error(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/BadName/instructions.md": "hi\n"})

    discovery = discover_agent(tmp_path / "agents" / "BadName")

    assert discovery.has_errors
    assert any("invalid agent id" in d.message for d in discovery.diagnostics)


def test_skills_subdir_without_skill_md_is_a_warning(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/skills/incomplete/notes.md": "oops, no SKILL.md\n",
        },
    )

    discovery = discover_agent(tmp_path / "agents" / "helper")

    assert discovery.skills == ()
    assert any(d.severity == "warning" and "incomplete" in d.message for d in discovery.diagnostics)


def test_skill_with_invalid_id_is_an_error(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/skills/Bad-Skill/SKILL.md": "---\nname: x\ndescription: y\n---\nbody\n",
        },
    )

    discovery = discover_agent(tmp_path / "agents" / "helper")

    assert discovery.skills == ()
    assert any(
        d.severity == "error" and "invalid skill id" in d.message for d in discovery.diagnostics
    )


def test_valid_skill_is_classified(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/skills/onboarding/SKILL.md": (
                "---\nname: Onboarding\ndescription: Helps onboard.\n---\nBody text.\n"
            ),
        },
    )

    discovery = discover_agent(tmp_path / "agents" / "helper")

    assert [s.skill_id for s in discovery.skills] == ["onboarding"]


def test_subagents_are_discovered_recursively(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/subagents/researcher/instructions.md": "you research things\n",
        },
    )

    discovery = discover_agent(tmp_path / "agents" / "helper")

    assert len(discovery.subagents) == 1
    assert discovery.subagents[0].agent_id == "researcher"
    assert not discovery.subagents[0].has_errors


def test_bundle_missing_bundle_yaml_is_an_error(tmp_path: Path) -> None:
    write_files(tmp_path, {"shared/utils/tools/x.py": "x = 1\n"})

    discovery = discover_bundle(tmp_path / "shared" / "utils")

    assert discovery.config_path is None
    assert discovery.has_errors
    assert any("bundle.yaml" in d.message for d in discovery.diagnostics)


def test_discover_fleet_finds_agents_and_bundles(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/one/instructions.md": "a\n",
            "agents/two/instructions.md": "b\n",
            "shared/utils/bundle.yaml": "description: shared utilities\n",
        },
    )

    fleet = discover_fleet(tmp_path)

    assert {a.agent_id for a in fleet.agents} == {"one", "two"}
    assert {b.bundle_id for b in fleet.bundles} == {"utils"}


def test_discover_fleet_tolerates_missing_agents_or_shared_dirs(tmp_path: Path) -> None:
    fleet = discover_fleet(tmp_path)

    assert fleet.agents == ()
    assert fleet.bundles == ()
