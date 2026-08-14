"""``examples/fleet`` is a real, committed fleet: it must always validate clean.

Two complementary checks: a direct call into ``run_validate`` (fast, and
gives a normal pytest failure with the report text on any regression), and a
subprocess smoke test of the actual ``knot validate`` CLI entry point people
run — the same ``uv run --offline`` fallback pattern used by
``test_authoring_validate.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from knot.authoring.validate import format_report, run_validate

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FLEET_ROOT = _REPO_ROOT / "examples" / "fleet"
_MANIFESTS_DIR = _REPO_ROOT / "examples" / "manifests"


def test_example_fleet_validates_clean_direct_call() -> None:
    result = run_validate(_FLEET_ROOT)

    report = format_report(result)
    assert result.failed_count == 0, report
    assert result.ok_count == 1
    assert result.exit_code == 0

    # Every authoring feature the fleet is meant to demonstrate actually
    # made it into the compiled manifest: own tool, bundle tool (each
    # approval policy), connection tool, skill, and subagent delegation.
    support = result.fleet.agents["support"]
    assert support.ok
    assert support.manifest is not None
    tool_names = {t.name for t in support.manifest.tools}
    assert tool_names == {
        "ask_user",
        "load_skill",
        "lookup_order",
        "researcher",
        "update_customer",
        "flag_account",
        "support_mcp__add",
    }
    by_name = {t.name: t for t in support.manifest.tools}
    assert by_name["update_customer"].approval == "always"
    assert by_name["flag_account"].approval == "once"
    assert by_name["support_mcp__add"].source == "connection:crm/support_mcp"
    assert "a" not in by_name["support_mcp__add"].input_schema.get("properties", {})
    assert [s.id for s in support.manifest.skills] == ["refund-policy"]
    assert support.manifest.subagent_ids == ["researcher"]

    researcher = support.subagents["researcher"]
    assert researcher.ok
    assert researcher.manifest is not None
    researcher_tools = {t.name: t for t in researcher.manifest.tools}
    assert researcher_tools["search_notes"].approval == "always"


def test_example_fleet_manifests_committed_and_drift_free() -> None:
    """``examples/manifests`` is committed alongside the fleet; it must not drift."""
    result = run_validate(_FLEET_ROOT, manifests_dir=_MANIFESTS_DIR, check=True)

    assert result.exit_code == 0, format_report(result)
    assert result.changed_manifests == []


def _run_knot_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the installed ``knot`` console script via ``uv run``.

    Falls back to ``python -m knot.server.cli`` when ``uv`` cannot reach the
    network (offline dev/test environments) so this still proves the CLI
    wiring works end to end.
    """
    env = {**os.environ, "UV_OFFLINE": "1"}
    result = subprocess.run(
        ["uv", "run", "--offline", "knot", *args],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=env,
        check=False,
    )
    if result.returncode != 0 and "hatchling" in (result.stderr or ""):
        result = subprocess.run(
            [sys.executable, "-m", "knot.server.cli", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    return result


def test_example_fleet_cli_validate_subprocess_exits_zero() -> None:
    result = _run_knot_cli("validate", "--root", "examples/fleet")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 agents ok, 0 failed" in result.stdout


def test_example_fleet_cli_validate_manifests_check_subprocess_exits_zero() -> None:
    result = _run_knot_cli(
        "validate", "--root", "examples/fleet", "--manifests", "examples/manifests", "--check"
    )

    assert result.returncode == 0, result.stdout + result.stderr
