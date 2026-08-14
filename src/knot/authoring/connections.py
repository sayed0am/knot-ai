"""Offline connection lifecycle: snapshot refresh, drift, and health.

Network happens ONLY here (and, at runtime, inside a live tool call — see
``knot.authoring.mcp_client``) — never during ``knot.authoring.compile``,
which resolves connections exclusively from the committed snapshot files
this module writes. Three entry points:

- :func:`refresh_snapshots`: connect to each targeted connection, list its
  live tools, intersect with its ``allow`` list, and overwrite its committed
  ``shared/<bundle>/snapshots/<connection>.json``. Reports what changed
  (added/removed/schema-changed tool names) and which agents that touches.
- :func:`check_connection_health`: the read-only counterpart — reconnect and
  diff live tools against the committed snapshot without writing anything.
- :func:`default_token_resolver`: the default ``TokenResolver`` (read
  ``config.auth_env`` from the environment), reused by both of the above and
  by ``AgentRuntime`` at runtime.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import mcp_types as types

from knot.authoring.compile import compile_fleet
from knot.authoring.config import ConnectionConfig, load_bundle_config
from knot.authoring.discovery import discover_fleet
from knot.authoring.mcp_client import (
    TokenResolver,
    TransportFactory,
    build_auth,
    flatten_exception_text,
    list_all_tools,
    open_mcp_session,
    scrub_secrets,
)

__all__ = [
    "ConnectionRefreshResult",
    "ConnectionTarget",
    "HealthReport",
    "RefreshReport",
    "check_connection_health",
    "default_token_resolver",
    "format_refresh_report",
    "refresh_snapshots",
]


def default_token_resolver(connection_name: str, config: ConnectionConfig) -> str | None:
    """Read ``config.auth_env`` from the environment.

    ``config.auth_env is None`` means unauthenticated — but ``build_auth``
    never even calls a resolver in that case, so this function only runs for
    connections that declared an ``auth_env``. A missing environment
    variable is a clear, named error, not a silently-unauthenticated call.
    """
    if config.auth_env is None:
        return None
    value = os.environ.get(config.auth_env)
    if value is None:
        raise ValueError(
            f"connection {connection_name!r}: environment variable "
            f"{config.auth_env!r} (declared as auth_env) is not set"
        )
    return value


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _snapshot_tool_dict(tool: types.Tool) -> dict:
    return {
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": tool.input_schema or {},
    }


def _write_snapshot(
    path: Path, *, connection: str, tools: list[types.Tool], captured_at: str
) -> None:
    payload = {
        "connection": connection,
        "capturedAt": captured_at,
        "tools": sorted((_snapshot_tool_dict(t) for t in tools), key=lambda t: t["name"]),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_committed_tools(path: Path) -> dict[str, dict] | None:
    """The previously committed snapshot's tools, keyed by name — ``None``
    when there is no (or no readable) committed snapshot yet."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    tools = data.get("tools") if isinstance(data, dict) else None
    if not isinstance(tools, list):
        return None
    return {t["name"]: t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)}


def _diff_tools(
    previous: dict[str, dict] | None, live_tools: list[types.Tool]
) -> tuple[list[str], list[str], list[str]]:
    """``(added, removed, schema_changed)`` tool names, ``previous`` (``None``
    for a brand-new snapshot) versus the freshly-listed ``live_tools``."""
    live_by_name = {t.name: _snapshot_tool_dict(t) for t in live_tools}
    if previous is None:
        return [], [], []
    added = sorted(set(live_by_name) - set(previous))
    removed = sorted(set(previous) - set(live_by_name))
    schema_changed = sorted(
        name
        for name in set(live_by_name) & set(previous)
        if (live_by_name[name]["description"], live_by_name[name]["inputSchema"])
        != (previous[name].get("description"), previous[name].get("inputSchema"))
    )
    return added, removed, schema_changed


@dataclass(frozen=True, slots=True)
class ConnectionTarget:
    """One connection a refresh/health call could act on."""

    bundle_id: str
    connection: str
    config: ConnectionConfig
    snapshot_path: Path


def _collect_targets(
    root: Path, *, bundle_id: str | None, connection: str | None
) -> list[ConnectionTarget]:
    discovery = discover_fleet(root)
    bundle_discoveries = {b.bundle_id: b for b in discovery.bundles}
    if bundle_id is not None and bundle_id not in bundle_discoveries:
        raise ValueError(f"unknown bundle {bundle_id!r} under {root}")

    targets: list[ConnectionTarget] = []
    for b_id, b_discovery in sorted(bundle_discoveries.items()):
        if bundle_id is not None and b_id != bundle_id:
            continue
        if b_discovery.config_path is None:
            continue
        config, _diagnostics = load_bundle_config(b_discovery.config_path, bundle_id=b_id)
        if config is None:
            continue
        for c_name, c_config in sorted(config.connections.items()):
            if connection is not None and c_name != connection:
                continue
            snapshot_path = b_discovery.path / "snapshots" / f"{c_name}.json"
            targets.append(
                ConnectionTarget(
                    bundle_id=b_id, connection=c_name, config=c_config, snapshot_path=snapshot_path
                )
            )

    if connection is not None and bundle_id is not None:
        if not any(t.connection == connection for t in targets):
            raise ValueError(f"unknown connection {connection!r} in bundle {bundle_id!r}")
    return targets


async def _list_live_tools(
    target: ConnectionTarget,
    *,
    token_resolver: TokenResolver,
    transport_factory: TransportFactory | None,
) -> list[types.Tool]:
    auth = build_auth(target.config, target.connection, token_resolver=token_resolver)
    async with open_mcp_session(
        target.config, auth=auth, transport_factory=transport_factory
    ) as session:
        return await list_all_tools(session)


def _intersect_allow(target: ConnectionTarget, tools: list[types.Tool]) -> list[types.Tool]:
    if target.config.allow is None:
        return tools
    allowed = set(target.config.allow)
    return [t for t in tools if t.name in allowed]


# ---------------------------------------------------------------------------
# refresh_snapshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectionRefreshResult:
    bundle_id: str
    connection: str
    status: Literal["ok", "unreachable"]
    is_new: bool = False
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    schema_changed: tuple[str, ...] = ()
    affected_agents: tuple[str, ...] = ()
    error: str | None = None

    @property
    def changed(self) -> bool:
        return self.is_new or bool(self.added or self.removed or self.schema_changed)


@dataclass(frozen=True, slots=True)
class RefreshReport:
    results: tuple[ConnectionRefreshResult, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return all(r.status == "ok" for r in self.results)


async def refresh_snapshots(
    root: Path,
    *,
    bundle_id: str | None = None,
    connection: str | None = None,
    token_resolver: TokenResolver = default_token_resolver,
    transport_factory: TransportFactory | None = None,
    captured_at: str | None = None,
) -> RefreshReport:
    """Refresh every targeted connection's committed snapshot.

    Writing happens per connection, independently: one connection's failure
    to connect never stops the rest (its result carries
    ``status="unreachable"`` and the batch's exit decision, made by the
    caller, is a simple "any unreachable?" check). Affected agents are
    computed once, after every snapshot in the batch has been written, by
    recompiling the fleet and reading which agents' manifests carry a tool
    sourced from the connection that changed.
    """
    targets = _collect_targets(root, bundle_id=bundle_id, connection=connection)
    results: list[ConnectionRefreshResult] = []

    for target in targets:
        previous = _read_committed_tools(target.snapshot_path)
        try:
            live_tools = await _list_live_tools(
                target, token_resolver=token_resolver, transport_factory=transport_factory
            )
        except Exception as exc:  # noqa: BLE001 - one connection's failure is a report row
            results.append(
                ConnectionRefreshResult(
                    bundle_id=target.bundle_id,
                    connection=target.connection,
                    status="unreachable",
                    is_new=previous is None,
                    error=scrub_secrets(flatten_exception_text(exc), []),
                )
            )
            continue

        selected = _intersect_allow(target, live_tools)
        when = captured_at if captured_at is not None else _utc_now_iso()
        _write_snapshot(
            target.snapshot_path, connection=target.connection, tools=selected, captured_at=when
        )
        added, removed, schema_changed = _diff_tools(previous, selected)
        results.append(
            ConnectionRefreshResult(
                bundle_id=target.bundle_id,
                connection=target.connection,
                status="ok",
                is_new=previous is None,
                added=tuple(added),
                removed=tuple(removed),
                schema_changed=tuple(schema_changed),
            )
        )

    if any(r.status == "ok" and r.changed for r in results):
        fleet = compile_fleet(root)
        final: list[ConnectionRefreshResult] = []
        for result in results:
            if result.status != "ok" or not result.changed:
                final.append(result)
                continue
            source = f"connection:{result.bundle_id}/{result.connection}"
            affected = sorted(
                agent_id
                for agent_id, compiled in fleet.agents.items()
                if compiled.ok
                and compiled.manifest is not None
                and any(t.source == source for t in compiled.manifest.tools)
            )
            final.append(replace(result, affected_agents=tuple(affected)))
        results = final

    return RefreshReport(results=tuple(results))


def format_refresh_report(report: RefreshReport) -> str:
    """Render a stable, human-readable report — one line per connection."""
    lines: list[str] = []
    for result in report.results:
        prefix = f"{result.bundle_id}/{result.connection}"
        if result.status == "unreachable":
            lines.append(f"{prefix}: UNREACHABLE ({result.error})")
            continue
        if result.is_new:
            total = len(result.added) + len(result.removed)
            lines.append(f"{prefix}: new snapshot ({total} tools)")
        elif not result.changed:
            lines.append(f"{prefix}: unchanged")
        else:
            parts = []
            if result.added:
                parts.append(f"added={list(result.added)}")
            if result.removed:
                parts.append(f"removed={list(result.removed)}")
            if result.schema_changed:
                parts.append(f"schema_changed={list(result.schema_changed)}")
            lines.append(f"{prefix}: {', '.join(parts)}")
        if result.affected_agents:
            lines.append(f"  affects agents: {', '.join(result.affected_agents)}")
    ok_count = sum(1 for r in report.results if r.status == "ok")
    failed_count = len(report.results) - ok_count
    lines.append(f"{ok_count} connection(s) refreshed, {failed_count} unreachable")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# check_connection_health
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HealthReport:
    bundle_id: str
    connection: str
    status: Literal["ok", "drift", "unreachable"]
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    schema_changed: tuple[str, ...] = ()
    error: str | None = None


async def check_connection_health(
    root: Path,
    bundle_id: str,
    connection_name: str,
    *,
    token_resolver: TokenResolver = default_token_resolver,
    transport_factory: TransportFactory | None = None,
) -> HealthReport:
    """Reconnect and diff live tools against the committed snapshot, without
    writing anything — the read-only counterpart to :func:`refresh_snapshots`
    for one connection."""
    targets = _collect_targets(root, bundle_id=bundle_id, connection=connection_name)
    if not targets:
        raise ValueError(f"unknown connection {connection_name!r} in bundle {bundle_id!r}")
    target = targets[0]

    previous = _read_committed_tools(target.snapshot_path)
    try:
        live_tools = await _list_live_tools(
            target, token_resolver=token_resolver, transport_factory=transport_factory
        )
    except Exception as exc:  # noqa: BLE001 - unreachable is a status, not a crash
        return HealthReport(
            bundle_id=bundle_id,
            connection=connection_name,
            status="unreachable",
            error=scrub_secrets(flatten_exception_text(exc), []),
        )

    selected = _intersect_allow(target, live_tools)
    added, removed, schema_changed = _diff_tools(previous, selected)
    status: Literal["ok", "drift"] = "drift" if (added or removed or schema_changed) else "ok"
    return HealthReport(
        bundle_id=bundle_id,
        connection=connection_name,
        status=status,
        added=tuple(added),
        removed=tuple(removed),
        schema_changed=tuple(schema_changed),
    )
