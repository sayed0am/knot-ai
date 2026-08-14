"""`knot validate`: diagnostics grouped by agent, drift reporting, exit codes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from authoring_fixtures import write_files

from knot.authoring.validate import format_report, run_validate

_REPO_ROOT = Path(__file__).resolve().parent.parent

_THREE_AGENT_FLEET = {
    "agents/alpha/instructions.md": "alpha instructions\n",
    "agents/beta/instructions.md": "beta instructions\n",
    # gamma has a broken tool module and will fail to compile.
    "agents/gamma/instructions.md": "gamma instructions\n",
    "agents/gamma/tools/broken.py": "raise RuntimeError('gamma is broken')\n",
}


def test_run_validate_direct_call_one_broken_agent_among_three(tmp_path: Path) -> None:
    write_files(tmp_path, _THREE_AGENT_FLEET)

    result = run_validate(tmp_path)

    assert result.ok_count == 2
    assert result.failed_count == 1
    assert result.exit_code == 1

    report = format_report(result)
    assert "gamma" in report
    assert "gamma is broken" in report
    assert "2 agents ok, 1 failed" in report
    # The two healthy agents contribute no diagnostic lines of their own.
    assert "alpha:" not in report
    assert "beta:" not in report


def test_run_validate_reports_manifest_drift_without_failing(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/solo/instructions.md": "hi\n"})
    manifests_dir = tmp_path / "_manifests"

    result = run_validate(tmp_path, manifests_dir=manifests_dir)

    assert result.exit_code == 0  # drift alone is not a failure
    assert result.changed_manifests == ["solo"]
    assert "changed manifests: solo" in format_report(result)


def test_run_validate_check_flag_promotes_drift_to_exit_2(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/solo/instructions.md": "hi\n"})
    manifests_dir = tmp_path / "_manifests"

    result = run_validate(tmp_path, manifests_dir=manifests_dir, check=True)

    assert result.exit_code == 2


def test_run_validate_write_then_check_is_clean(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/solo/instructions.md": "hi\n"})
    manifests_dir = tmp_path / "_manifests"

    run_validate(tmp_path, manifests_dir=manifests_dir, write=True)
    result = run_validate(tmp_path, manifests_dir=manifests_dir, check=True)

    assert result.changed_manifests == []
    assert result.exit_code == 0


def test_run_validate_compile_failure_outranks_check_drift(tmp_path: Path) -> None:
    write_files(tmp_path, _THREE_AGENT_FLEET)
    manifests_dir = tmp_path / "_manifests"

    result = run_validate(tmp_path, manifests_dir=manifests_dir, check=True)

    assert result.exit_code == 1  # not 2: a compile failure always wins


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


def test_cli_validate_subprocess_one_broken_agent_among_three(tmp_path: Path) -> None:
    write_files(tmp_path, _THREE_AGENT_FLEET)

    result = _run_knot_cli("validate", "--root", str(tmp_path))

    assert result.returncode == 1
    assert "gamma" in result.stdout
    assert "2 agents ok, 1 failed" in result.stdout


def test_cli_validate_subprocess_all_ok(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/alpha/instructions.md": "alpha\n",
            "agents/beta/instructions.md": "beta\n",
        },
    )

    result = _run_knot_cli("validate", "--root", str(tmp_path))

    assert result.returncode == 0
    assert "2 agents ok, 0 failed" in result.stdout
