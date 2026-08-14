"""``knot validate``: compile a fleet and report diagnostics and manifest drift.

Kept separate from ``knot.server.cli`` so the CLI stays a thin argument-parsing
shell: everything decidable without a terminal (what failed, what changed,
what exit code to use) lives here and is directly callable from tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from knot.authoring.compile import CompiledFleet, compile_fleet, diff_manifests, write_manifests


@dataclass(frozen=True, slots=True)
class ValidateResult:
    fleet: CompiledFleet
    ok_count: int
    failed_count: int
    changed_manifests: list[str] | None
    exit_code: int


def run_validate(
    root: Path,
    *,
    manifests_dir: Path | None = None,
    write: bool = False,
    check: bool = False,
) -> ValidateResult:
    """Compile every agent under ``root`` and decide the CLI's outcome.

    ``manifests_dir`` given: also computes the manifest drift list (before
    any ``write``, so it always answers "what would/did change"). ``write``
    persists fresh manifests to ``manifests_dir`` after computing that diff.
    ``check`` turns non-empty drift into exit code 2 — but only when the
    fleet itself compiled cleanly; a compile failure (exit 1) always wins.
    """
    fleet = compile_fleet(root)

    changed: list[str] | None = None
    if manifests_dir is not None:
        changed = diff_manifests(fleet, manifests_dir)
        if write:
            write_manifests(fleet, manifests_dir)

    ok_count = sum(1 for compiled in fleet.agents.values() if compiled.ok)
    failed_count = len(fleet.agents) - ok_count

    if failed_count > 0:
        exit_code = 1
    elif check and changed:
        exit_code = 2
    else:
        exit_code = 0

    return ValidateResult(
        fleet=fleet,
        ok_count=ok_count,
        failed_count=failed_count,
        changed_manifests=changed,
        exit_code=exit_code,
    )


def format_report(result: ValidateResult) -> str:
    """Render a stable, human-readable report: per-agent diagnostics (errors
    before warnings), then bundle diagnostics, then the summary line."""
    lines: list[str] = []

    for agent_id in sorted(result.fleet.agents):
        compiled = result.fleet.agents[agent_id]
        if not compiled.diagnostics:
            continue
        lines.append(f"{agent_id}:")
        errors = [d for d in compiled.diagnostics if d.severity == "error"]
        warnings = [d for d in compiled.diagnostics if d.severity == "warning"]
        for diagnostic in [*errors, *warnings]:
            lines.append(f"  {diagnostic}")

    for diagnostic in result.fleet.bundle_diagnostics:
        label = f"bundle:{diagnostic.bundle_id}" if diagnostic.bundle_id else "bundle"
        lines.append(f"{label}: {diagnostic}")

    lines.append(f"{result.ok_count} agents ok, {result.failed_count} failed")

    if result.changed_manifests:
        lines.append(f"changed manifests: {', '.join(result.changed_manifests)}")

    return "\n".join(lines)


__all__ = ["ValidateResult", "format_report", "run_validate"]
