"""Offline connection lifecycle: snapshot refresh, drift, and health."""

from __future__ import annotations

import json
from pathlib import Path

from authoring_fixtures import write_files
from mcp_fixtures import asgi_transport_factory, mock_mcp_server

from knot.authoring.connections import (
    check_connection_health,
    format_refresh_report,
    refresh_snapshots,
)


def _write_bundle_with_connection(root: Path, *, bundle_id: str, url: str, allow=None) -> None:
    allow_line = f"    allow: {allow}\n" if allow is not None else ""
    write_files(
        root,
        {
            f"shared/{bundle_id}/bundle.yaml": (
                "connections:\n"
                "  api:\n"
                f"    url: {url}\n"
                "    transport: streamable_http\n" + allow_line
            ),
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": f"use: [{bundle_id}]\n",
        },
    )


async def test_refresh_writes_deterministic_snapshot_byte_identical_on_rerefresh(
    tmp_path: Path,
) -> None:
    async with mock_mcp_server() as handle:
        _write_bundle_with_connection(tmp_path, bundle_id="svc", url=handle.url)
        tf = asgi_transport_factory(handle)

        report1 = await refresh_snapshots(
            tmp_path, transport_factory=tf, captured_at="2026-01-01T00:00:00Z"
        )
        assert report1.ok
        assert report1.results[0].is_new is True

        snapshot_path = tmp_path / "shared" / "svc" / "snapshots" / "api.json"
        first_bytes = snapshot_path.read_bytes()

        report2 = await refresh_snapshots(
            tmp_path, transport_factory=tf, captured_at="2026-01-01T00:00:00Z"
        )
        second_bytes = snapshot_path.read_bytes()

        assert first_bytes == second_bytes
        assert first_bytes.endswith(b"\n")
        assert report2.results[0].changed is False

        data = json.loads(first_bytes)
        assert data["connection"] == "api"
        assert {t["name"] for t in data["tools"]} == {
            "echo",
            "add",
            "big_result",
            "trigger_list_changed",
        }


async def test_refresh_allow_list_intersection(tmp_path: Path) -> None:
    async with mock_mcp_server() as handle:
        _write_bundle_with_connection(
            tmp_path, bundle_id="svc", url=handle.url, allow=["echo", "add"]
        )
        report = await refresh_snapshots(
            tmp_path, transport_factory=asgi_transport_factory(handle), captured_at="t"
        )
        assert report.ok
        data = json.loads((tmp_path / "shared" / "svc" / "snapshots" / "api.json").read_text())
        assert {t["name"] for t in data["tools"]} == {"echo", "add"}


async def test_refresh_reports_added_removed_schema_changed(tmp_path: Path) -> None:
    async with mock_mcp_server() as handle:
        _write_bundle_with_connection(tmp_path, bundle_id="svc", url=handle.url)
        tf = asgi_transport_factory(handle)

        snapshot_path = tmp_path / "shared" / "svc" / "snapshots" / "api.json"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(
            json.dumps(
                {
                    "connection": "api",
                    "capturedAt": "x",
                    "tools": [
                        {"name": "echo", "description": "OLD DESCRIPTION", "inputSchema": {}},
                        {"name": "retired_tool", "description": "d", "inputSchema": {}},
                    ],
                }
            ),
            encoding="utf-8",
        )

        report = await refresh_snapshots(tmp_path, transport_factory=tf, captured_at="t2")
        result = report.results[0]
        assert result.status == "ok"
        assert "retired_tool" in result.removed
        assert "add" in result.added
        assert "echo" in result.schema_changed
        assert "helper" in result.affected_agents


async def test_refresh_unreachable_connection_reports_error_without_writing(tmp_path: Path) -> None:
    _write_bundle_with_connection(tmp_path, bundle_id="svc", url="http://127.0.0.1:1/mcp")

    report = await refresh_snapshots(tmp_path, captured_at="t")
    assert report.ok is False
    assert report.results[0].status == "unreachable"
    assert not (tmp_path / "shared" / "svc" / "snapshots" / "api.json").exists()


async def test_refresh_scoped_to_bundle_and_connection(tmp_path: Path) -> None:
    async with mock_mcp_server() as handle1, mock_mcp_server() as handle2:
        write_files(
            tmp_path,
            {
                "shared/svc1/bundle.yaml": (
                    f"connections:\n  api:\n    url: {handle1.url}\n"
                    "    transport: streamable_http\n"
                ),
                "shared/svc2/bundle.yaml": (
                    f"connections:\n  api:\n    url: {handle2.url}\n"
                    "    transport: streamable_http\n"
                ),
                "agents/helper/instructions.md": "hi\n",
                "agents/helper/agent.yaml": "use: [svc1, svc2]\n",
            },
        )

        def tf(url: str):
            if url == handle1.url:
                return handle1.transport_factory(url)
            return handle2.transport_factory(url)

        report = await refresh_snapshots(
            tmp_path, bundle_id="svc1", transport_factory=tf, captured_at="t"
        )
        assert len(report.results) == 1
        assert report.results[0].bundle_id == "svc1"
        assert not (tmp_path / "shared" / "svc2" / "snapshots" / "api.json").exists()


async def test_health_check_reports_drift_without_writing(tmp_path: Path) -> None:
    async with mock_mcp_server() as handle:
        _write_bundle_with_connection(tmp_path, bundle_id="svc", url=handle.url)
        snapshot_path = tmp_path / "shared" / "svc" / "snapshots" / "api.json"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(
            json.dumps(
                {
                    "connection": "api",
                    "capturedAt": "x",
                    "tools": [
                        {"name": "only_this", "description": "d", "inputSchema": {}}
                    ],
                }
            ),
            encoding="utf-8",
        )
        before = snapshot_path.read_bytes()

        health = await check_connection_health(
            tmp_path, "svc", "api", transport_factory=asgi_transport_factory(handle)
        )
        assert health.status == "drift"
        assert "echo" in health.added
        assert "only_this" in health.removed
        assert snapshot_path.read_bytes() == before  # health never writes


def test_format_refresh_report_is_stable_text() -> None:
    from knot.authoring.connections import ConnectionRefreshResult, RefreshReport

    report = RefreshReport(
        results=(
            ConnectionRefreshResult(
                bundle_id="svc", connection="api", status="ok", added=("new_tool",),
                affected_agents=("helper",),
            ),
            ConnectionRefreshResult(
                bundle_id="svc", connection="down", status="unreachable", error="boom"
            ),
        )
    )
    text = format_refresh_report(report)
    assert "svc/api" in text
    assert "new_tool" in text
    assert "affects agents: helper" in text
    assert "svc/down: UNREACHABLE" in text
    assert "1 connection(s) refreshed, 1 unreachable" in text
