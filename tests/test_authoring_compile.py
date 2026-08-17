"""Phase-2 compile: bundle resolution, connection snapshots, collisions, floor tools."""

from __future__ import annotations

import json
from pathlib import Path

from authoring_fixtures import write_files

from knot.authoring.compile import compile_fleet
from knot.core.tools import execute_tool
from knot.providers.messages import ToolCall

_TOOL_SRC = """
    from knot.authoring.tools import tool


    @tool
    def {name}(x: int) -> int:
        \"\"\"A tiny test tool.\"\"\"
        return x
"""


def test_import_error_fails_only_that_agent(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/good/instructions.md": "hi\n",
            "agents/good/tools/ok.py": _TOOL_SRC.format(name="ok"),
            "agents/broken/instructions.md": "hi\n",
            "agents/broken/tools/broken.py": "raise RuntimeError('boom at import time')\n",
        },
    )

    fleet = compile_fleet(tmp_path)

    assert fleet.agents["good"].ok is True
    assert fleet.agents["good"].manifest is not None
    assert fleet.agents["broken"].ok is False
    assert fleet.agents["broken"].manifest is None
    messages = [d.message for d in fleet.agents["broken"].diagnostics]
    assert any("broken.py" in m and "boom at import time" in m for m in messages)


def test_bundle_resolution_pulls_tools_skills_and_approvals_atomically(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "shared/utils/bundle.yaml": "approvals:\n  shared_tool: once\n",
            "shared/utils/tools/shared_tool.py": _TOOL_SRC.format(name="shared_tool"),
            "shared/utils/skills/onboarding/SKILL.md": (
                "---\nname: Onboarding\ndescription: Gets you started.\n---\nBody.\n"
            ),
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": "use: [utils]\n",
        },
    )

    fleet = compile_fleet(tmp_path)
    compiled = fleet.agents["helper"]

    assert compiled.ok is True
    manifest = compiled.manifest
    assert manifest is not None
    tool_names = {t.name for t in manifest.tools}
    assert "shared_tool" in tool_names
    assert "load_skill" in tool_names  # present because a skill arrived via the bundle
    shared_tool = next(t for t in manifest.tools if t.name == "shared_tool")
    assert shared_tool.source == "bundle:utils"
    assert shared_tool.approval == "once"
    skill_ids = {s.id for s in manifest.skills}
    assert skill_ids == {"onboarding"}


def test_bundle_with_missing_snapshot_fails_atomically_no_partial_tools(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "shared/utils/bundle.yaml": (
                "connections:\n"
                "  billing:\n"
                "    url: https://billing.example.internal/mcp\n"
                "    transport: streamable_http\n"
            ),
            "shared/utils/tools/shared_tool.py": _TOOL_SRC.format(name="shared_tool"),
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": "use: [utils]\n",
        },
    )

    fleet = compile_fleet(tmp_path)

    assert any("snapshot" in d.message and d.bundle_id == "utils" for d in fleet.bundle_diagnostics)
    compiled = fleet.agents["helper"]
    assert compiled.ok is False
    assert any("utils" in d.message for d in compiled.diagnostics)


def _write_snapshot(bundle_dir: Path, connection: str, tools: list[dict]) -> None:
    snapshot_dir = bundle_dir / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / f"{connection}.json").write_text(
        json.dumps({"connection": connection, "capturedAt": 0, "tools": tools}), encoding="utf-8"
    )


def test_snapshot_tools_land_qualified_and_execute_less_with_allow_intersection(
    tmp_path: Path,
) -> None:
    write_files(
        tmp_path,
        {
            "shared/billing/bundle.yaml": (
                "connections:\n"
                "  billing:\n"
                "    url: https://billing.example.internal/mcp\n"
                "    transport: streamable_http\n"
                "    allow: [get_invoice, nonexistent_tool]\n"
            ),
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": "use: [billing]\n",
        },
    )
    _write_snapshot(
        tmp_path / "shared" / "billing",
        "billing",
        [
            {
                "name": "get_invoice",
                "description": "look up an invoice",
                "inputSchema": {"type": "object"},
            },
            {
                "name": "charge_card",
                "description": "charge a card",
                "inputSchema": {"type": "object"},
            },
        ],
    )

    fleet = compile_fleet(tmp_path)
    compiled = fleet.agents["helper"]

    assert compiled.ok is True
    manifest = compiled.manifest
    assert manifest is not None
    tool_names = {t.name for t in manifest.tools}
    assert "billing__get_invoice" in tool_names
    assert "billing__charge_card" not in tool_names  # not allow-listed

    get_invoice = next(t for t in manifest.tools if t.name == "billing__get_invoice")
    assert get_invoice.source == "connection:billing/billing"
    assert get_invoice.executable is False

    warnings = [d for d in fleet.bundle_diagnostics if d.severity == "warning"]
    assert any("nonexistent_tool" in d.message for d in warnings)

    live_tool = compiled.tools["billing__get_invoice"]
    assert live_tool.execute_fn is None


def test_snapshot_tools_default_to_all_when_no_allow_list(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "shared/billing/bundle.yaml": (
                "connections:\n"
                "  billing:\n"
                "    url: https://billing.example.internal/mcp\n"
                "    transport: streamable_http\n"
            ),
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": "use: [billing]\n",
        },
    )
    _write_snapshot(
        tmp_path / "shared" / "billing",
        "billing",
        [{"name": "get_invoice", "description": "d", "inputSchema": {}}],
    )

    fleet = compile_fleet(tmp_path)
    manifest = fleet.agents["helper"].manifest
    assert manifest is not None
    # ask_user and read_tool_output are the always-on framework-floor tools;
    # billing__get_invoice is the only connection tool that survived the
    # allow list.
    assert {t.name for t in manifest.tools} == {
        "billing__get_invoice",
        "ask_user",
        "read_tool_output",
    }


def test_provided_arguments_stripped_from_model_facing_schema(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "shared/billing/bundle.yaml": (
                "connections:\n"
                "  billing:\n"
                "    url: https://billing.example.internal/mcp\n"
                "    transport: streamable_http\n"
                "    provided_arguments:\n"
                "      account_id: current_account_id\n"
            ),
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": "use: [billing]\n",
        },
    )
    _write_snapshot(
        tmp_path / "shared" / "billing",
        "billing",
        [
            {
                "name": "get_invoice",
                "description": "look up an invoice",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string"},
                        "invoice_id": {"type": "string"},
                    },
                    "required": ["account_id", "invoice_id"],
                },
            }
        ],
    )

    fleet = compile_fleet(tmp_path)
    compiled = fleet.agents["helper"]
    assert compiled.ok is True

    manifest_tool = next(t for t in compiled.manifest.tools if t.name == "billing__get_invoice")
    schema = manifest_tool.input_schema
    assert "account_id" not in schema["properties"]
    assert "invoice_id" in schema["properties"]
    assert schema["required"] == ["invoice_id"]

    # The live compiled AgentTool (what the runtime later wires an executor
    # onto) carries the same stripped schema — one source of truth.
    live_tool = compiled.tools["billing__get_invoice"]
    assert "account_id" not in live_tool.parameters["properties"]


def test_provided_arguments_key_absent_from_schema_warns(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "shared/billing/bundle.yaml": (
                "connections:\n"
                "  billing:\n"
                "    url: https://billing.example.internal/mcp\n"
                "    transport: streamable_http\n"
                "    provided_arguments:\n"
                "      nonexistent_arg: some_resolver_key\n"
            ),
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": "use: [billing]\n",
        },
    )
    _write_snapshot(
        tmp_path / "shared" / "billing",
        "billing",
        [{"name": "get_invoice", "description": "d", "inputSchema": {"type": "object"}}],
    )

    fleet = compile_fleet(tmp_path)
    assert fleet.agents["helper"].ok is True
    warnings = [d for d in fleet.bundle_diagnostics if d.severity == "warning"]
    assert any("nonexistent_arg" in d.message and "get_invoice" in d.message for d in warnings)


def test_agent_approval_overrides_bundle_approval_for_same_tool(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "shared/utils/bundle.yaml": "approvals:\n  shared_tool: always\n",
            "shared/utils/tools/shared_tool.py": _TOOL_SRC.format(name="shared_tool"),
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": "use: [utils]\napprovals:\n  shared_tool: never\n",
        },
    )

    fleet = compile_fleet(tmp_path)
    manifest = fleet.agents["helper"].manifest
    assert manifest is not None
    shared_tool = next(t for t in manifest.tools if t.name == "shared_tool")
    assert shared_tool.approval == "never"


def test_approval_object_form_carries_ttl_into_the_manifest(tmp_path: Path) -> None:
    """Design D7: the object form (``{policy, ttl_seconds}``) resolves to
    the same ``approval`` policy as the bare form, plus a ``ttl_seconds``
    the bare form never sets."""
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": (
                "approvals:\n  shared_tool:\n    policy: always\n    ttl_seconds: 3600\n"
            ),
            "agents/helper/tools/shared_tool.py": _TOOL_SRC.format(name="shared_tool"),
        },
    )

    fleet = compile_fleet(tmp_path)
    manifest = fleet.agents["helper"].manifest
    assert manifest is not None
    shared_tool = next(t for t in manifest.tools if t.name == "shared_tool")
    assert shared_tool.approval == "always"
    assert shared_tool.ttl_seconds == 3600


def test_approval_bare_form_leaves_ttl_seconds_none(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": "approvals:\n  shared_tool: always\n",
            "agents/helper/tools/shared_tool.py": _TOOL_SRC.format(name="shared_tool"),
        },
    )

    fleet = compile_fleet(tmp_path)
    manifest = fleet.agents["helper"].manifest
    assert manifest is not None
    shared_tool = next(t for t in manifest.tools if t.name == "shared_tool")
    assert shared_tool.approval == "always"
    assert shared_tool.ttl_seconds is None


def test_approval_for_unknown_tool_is_a_warning(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": "approvals:\n  nonexistent_tool: always\n",
        },
    )

    fleet = compile_fleet(tmp_path)
    compiled = fleet.agents["helper"]
    assert compiled.ok is True
    assert any(
        d.severity == "warning" and "nonexistent_tool" in d.message for d in compiled.diagnostics
    )


def test_authored_vs_bundle_collision_is_an_error_naming_both(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "shared/utils/bundle.yaml": "description: utils\n",
            "shared/utils/tools/shared_tool.py": _TOOL_SRC.format(name="shared_tool"),
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": "use: [utils]\n",
            "agents/helper/tools/shared_tool.py": _TOOL_SRC.format(name="shared_tool"),
        },
    )

    fleet = compile_fleet(tmp_path)
    compiled = fleet.agents["helper"]

    assert compiled.ok is False
    collision = next(d for d in compiled.diagnostics if "collision" in d.message)
    assert "authored" in collision.message
    assert "bundle:utils" in collision.message


def test_unknown_bundle_reference_is_an_error(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": "use: [does_not_exist]\n",
        },
    )

    fleet = compile_fleet(tmp_path)
    compiled = fleet.agents["helper"]

    assert compiled.ok is False
    assert any("does_not_exist" in d.message for d in compiled.diagnostics)


def test_load_skill_absent_when_no_skills(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/helper/instructions.md": "hi\n"})

    fleet = compile_fleet(tmp_path)
    manifest = fleet.agents["helper"].manifest
    assert manifest is not None
    assert manifest.skills == []
    assert all(t.name != "load_skill" for t in manifest.tools)


async def test_load_skill_present_and_functional_when_skills_exist(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/skills/onboarding/SKILL.md": (
                "---\nname: Onboarding\ndescription: Gets you started.\n---\nStep one, step two.\n"
            ),
        },
    )

    fleet = compile_fleet(tmp_path)
    compiled = fleet.agents["helper"]
    manifest = compiled.manifest
    assert manifest is not None
    assert any(t.name == "load_skill" for t in manifest.tools)

    load_skill_tool = compiled.tools["load_skill"]
    call = ToolCall(id="c1", name="load_skill", arguments={"skill": "onboarding"})
    result, is_error = await execute_tool(load_skill_tool, call)
    assert is_error is False
    assert result.text == "Step one, step two."


def test_ask_user_present_unconditionally(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/helper/instructions.md": "hi\n"})

    fleet = compile_fleet(tmp_path)
    manifest = fleet.agents["helper"].manifest
    assert manifest is not None
    ask_user = next(t for t in manifest.tools if t.name == "ask_user")
    assert ask_user.source == "builtin"
    assert ask_user.executable is False


def test_authored_ask_user_collision_is_an_error_naming_both_sources(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/tools/ask_user.py": _TOOL_SRC.format(name="ask_user"),
        },
    )

    fleet = compile_fleet(tmp_path)
    compiled = fleet.agents["helper"]

    assert compiled.ok is False
    collision = next(d for d in compiled.diagnostics if "collision" in d.message)
    assert "ask_user" in collision.message
    assert "authored" in collision.message
    assert "builtin" in collision.message


def test_subagent_is_lowered_into_a_manifest_delegation_tool(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/subagents/researcher/instructions.md": "you research\n",
            "agents/helper/subagents/researcher/agent.yaml": "description: Digs up facts.\n",
        },
    )

    fleet = compile_fleet(tmp_path)
    compiled = fleet.agents["helper"]
    assert compiled.ok is True
    manifest = compiled.manifest
    assert manifest is not None
    assert manifest.subagent_ids == ["researcher"]

    tool = next(t for t in manifest.tools if t.name == "researcher")
    assert tool.source == "subagent"
    assert tool.description == "Digs up facts."
    assert tool.executable is True
    assert tool.input_schema == {
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

    # No live executable tool for it here: an execute-less AgentTool would
    # wrongly park as a question instead of actually delegating; the real
    # delegation executor is assembled at runtime.
    assert "researcher" not in compiled.tools
    assert "researcher" in compiled.subagents
    assert compiled.subagents["researcher"].ok is True
    assert compiled.subagents["researcher"].manifest is not None


def test_subagent_missing_description_fails_parent_compile(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/subagents/researcher/instructions.md": "you research\n",
        },
    )

    fleet = compile_fleet(tmp_path)
    compiled = fleet.agents["helper"]

    assert compiled.ok is False
    assert compiled.manifest is None
    assert any(
        "researcher" in d.message and "description" in d.message for d in compiled.diagnostics
    )


def test_subagent_own_compile_error_fails_parent_with_prefixed_diagnostics(
    tmp_path: Path,
) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/subagents/researcher/instructions.md": "you research\n",
            "agents/helper/subagents/researcher/agent.yaml": "description: Digs up facts.\n",
            "agents/helper/subagents/researcher/tools/broken.py": "raise RuntimeError('boom')\n",
        },
    )

    fleet = compile_fleet(tmp_path)
    compiled = fleet.agents["helper"]

    assert compiled.ok is False
    assert any("researcher" in d.message and "boom" in d.message for d in compiled.diagnostics)


def test_subagent_name_collision_with_authored_tool_is_an_error_naming_both(
    tmp_path: Path,
) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/tools/researcher.py": _TOOL_SRC.format(name="researcher"),
            "agents/helper/subagents/researcher/instructions.md": "you research\n",
            "agents/helper/subagents/researcher/agent.yaml": "description: Digs up facts.\n",
        },
    )

    fleet = compile_fleet(tmp_path)
    compiled = fleet.agents["helper"]

    assert compiled.ok is False
    collision = next(d for d in compiled.diagnostics if "collision" in d.message)
    assert "researcher" in collision.message
    assert "authored" in collision.message
    assert "subagent" in collision.message


def test_nested_subagent_of_subagent_compiles(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/subagents/researcher/instructions.md": "you research\n",
            "agents/helper/subagents/researcher/agent.yaml": "description: Digs up facts.\n",
            "agents/helper/subagents/researcher/subagents/fact_checker/instructions.md": "check\n",
            "agents/helper/subagents/researcher/subagents/fact_checker/agent.yaml": (
                "description: Verifies claims.\n"
            ),
        },
    )

    fleet = compile_fleet(tmp_path)
    compiled = fleet.agents["helper"]
    assert compiled.ok is True

    researcher = compiled.subagents["researcher"]
    assert researcher.ok is True
    assert researcher.manifest is not None
    assert researcher.manifest.subagent_ids == ["fact_checker"]
    assert any(t.name == "fact_checker" for t in researcher.manifest.tools)
    assert "fact_checker" in researcher.subagents


def test_thinking_budget_on_anthropic_provider_compiles(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": (
                "model:\n"
                "  provider: anthropic\n"
                "  name: claude-test\n"
                "  thinking_budget_tokens: 2048\n"
            ),
        },
    )

    fleet = compile_fleet(tmp_path)
    compiled = fleet.agents["helper"]
    assert compiled.ok is True
    assert compiled.manifest is not None
    assert compiled.manifest.model is not None
    assert compiled.manifest.model.thinking_budget_tokens == 2048


def test_thinking_budget_on_non_anthropic_provider_is_a_compile_error(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": (
                "model:\n  provider: openai\n  name: gpt-test\n  thinking_budget_tokens: 2048\n"
            ),
        },
    )

    fleet = compile_fleet(tmp_path)
    compiled = fleet.agents["helper"]
    assert compiled.ok is False
    diagnostic = next(d for d in compiled.diagnostics if "thinking_budget_tokens" in d.message)
    assert diagnostic.severity == "error"
    assert "openai" in diagnostic.message
