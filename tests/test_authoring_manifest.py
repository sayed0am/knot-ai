"""Manifest determinism, hashing, and write/diff blast-radius reporting."""

from __future__ import annotations

from pathlib import Path

from authoring_fixtures import write_files

from knot.authoring.compile import compile_fleet, diff_manifests, write_manifests
from knot.authoring.manifest import serialize_manifest

_FLEET = {
    "shared/utils/bundle.yaml": "description: utils\n",
    "shared/utils/tools/shared_tool.py": (
        "from knot.authoring.tools import tool\n\n\n"
        "@tool\n"
        "def shared_tool(x: int) -> int:\n"
        '    """A shared tool."""\n'
        "    return x\n"
    ),
    "agents/helper/instructions.md": "You are helpful.\n",
    "agents/helper/agent.yaml": "use: [utils]\n",
    "agents/other/instructions.md": "You do other things.\n",
    "agents/other/agent.yaml": "use: [utils]\n",
    "agents/standalone/instructions.md": "I use nothing shared.\n",
}


def test_compiling_twice_produces_byte_identical_manifests(tmp_path: Path) -> None:
    write_files(tmp_path, _FLEET)

    first = compile_fleet(tmp_path)
    second = compile_fleet(tmp_path)

    manifest_a = first.agents["helper"].manifest
    manifest_b = second.agents["helper"].manifest
    assert manifest_a is not None
    assert manifest_b is not None
    assert serialize_manifest(manifest_a) == serialize_manifest(manifest_b)


def test_instructions_sha256_changes_when_instructions_change(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/helper/instructions.md": "version one\n"})
    first = compile_fleet(tmp_path).agents["helper"].manifest
    assert first is not None

    write_files(tmp_path, {"agents/helper/instructions.md": "version two, different text\n"})
    second = compile_fleet(tmp_path).agents["helper"].manifest
    assert second is not None

    assert first.instructions_sha256 != second.instructions_sha256


def test_manifest_shape_has_expected_top_level_keys(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/helper/instructions.md": "hi\n"})
    manifest = compile_fleet(tmp_path).agents["helper"].manifest
    assert manifest is not None

    import json

    data = json.loads(serialize_manifest(manifest))
    assert data["agentId"] == "helper"
    assert data["instructionsPath"] == "agents/helper/instructions.md"
    assert "instructionsSha256" in data
    # ask_user is the always-on framework-floor tool; it is the only tool an
    # otherwise-empty agent gets.
    assert len(data["tools"]) == 1
    ask_user = data["tools"][0]
    assert ask_user["name"] == "ask_user"
    assert ask_user["source"] == "builtin"
    assert ask_user["executable"] is False
    assert data["skills"] == []
    assert data["subagentIds"] == []
    assert data["use"] == []
    # Nested limits/model must be camelCase too: the manifest is a single,
    # uniformly wire-cased artifact, not just camelCase at the top level.
    assert data["limits"] == {
        "maxTurns": None,
        "maxResultBytes": None,
        "delegationMaxPerTurn": 4,
        "delegationMaxConcurrent": 2,
    }
    assert "max_turns" not in data["limits"]
    assert "delegation_max_per_turn" not in data["limits"]


def test_manifest_model_config_is_also_camel_case(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": (
                "model:\n"
                "  provider: anthropic\n"
                "  name: some-model\n"
                "  max_tokens: 4096\n"
                "limits:\n"
                "  max_turns: 20\n"
                "  delegation_max_per_turn: 2\n"
            ),
        },
    )

    manifest = compile_fleet(tmp_path).agents["helper"].manifest
    assert manifest is not None
    # Python attribute access stays snake_case regardless of wire casing.
    assert manifest.model is not None
    assert manifest.model.max_tokens == 4096
    assert manifest.limits.delegation_max_per_turn == 2

    import json

    data = json.loads(serialize_manifest(manifest))
    assert data["model"] == {"provider": "anthropic", "name": "some-model", "maxTokens": 4096}
    assert data["limits"]["maxTurns"] == 20
    assert data["limits"]["delegationMaxPerTurn"] == 2
    assert "max_tokens" not in data["model"]
    assert "delegation_max_per_turn" not in data["limits"]


def test_write_then_diff_bundle_tool_edit_flags_only_referencing_agents(tmp_path: Path) -> None:
    write_files(tmp_path, _FLEET)
    manifests_dir = tmp_path / "_manifests"

    fleet = compile_fleet(tmp_path)
    write_manifests(fleet, manifests_dir)

    # Nothing changed yet.
    assert diff_manifests(compile_fleet(tmp_path), manifests_dir) == []

    # Edit the shared tool's description: only agents referencing the bundle
    # that owns it should show up as changed.
    write_files(
        tmp_path,
        {
            "shared/utils/tools/shared_tool.py": (
                "from knot.authoring.tools import tool\n\n\n"
                "@tool\n"
                "def shared_tool(x: int) -> int:\n"
                '    """A shared tool, now with an updated description."""\n'
                "    return x\n"
            )
        },
    )

    changed = diff_manifests(compile_fleet(tmp_path), manifests_dir)

    assert changed == ["helper", "other"]


def test_diff_reports_missing_committed_manifest(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/helper/instructions.md": "hi\n"})
    fleet = compile_fleet(tmp_path)

    assert diff_manifests(fleet, tmp_path / "_manifests") == ["helper"]


def test_diff_reports_extra_committed_manifest_for_deleted_agent(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/helper/instructions.md": "hi\n"})
    manifests_dir = tmp_path / "_manifests"
    write_manifests(compile_fleet(tmp_path), manifests_dir)

    # The agent directory disappears entirely.
    import shutil

    shutil.rmtree(tmp_path / "agents" / "helper")

    changed = diff_manifests(compile_fleet(tmp_path), manifests_dir)

    assert changed == ["helper"]


def test_write_manifests_creates_output_directory(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/helper/instructions.md": "hi\n"})
    manifests_dir = tmp_path / "nested" / "_manifests"

    write_manifests(compile_fleet(tmp_path), manifests_dir)

    assert (manifests_dir / "helper.json").is_file()
