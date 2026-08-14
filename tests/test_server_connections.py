"""``GET /connections/health``."""

from __future__ import annotations

from pathlib import Path

from authoring_fixtures import write_files
from mcp_fixtures import asgi_transport_factory, mock_mcp_server
from server_fixtures import client_for, make_app

from knot.authoring.connections import refresh_snapshots


def _write_bundle(tmp_path: Path, *, url: str) -> None:
    write_files(
        tmp_path,
        {
            "shared/svc/bundle.yaml": (
                f"connections:\n  crm:\n    url: {url}\n    transport: streamable_http\n"
            ),
            "agents/root/instructions.md": "you help with billing\n",
            "agents/root/agent.yaml": "use: [svc]\n",
        },
    )


async def test_connections_health_ok(tmp_path: Path) -> None:
    async with mock_mcp_server() as handle:
        tf = asgi_transport_factory(handle)
        _write_bundle(tmp_path, url=handle.url)
        await refresh_snapshots(tmp_path, transport_factory=tf, captured_at="t")

        app = make_app(tmp_path, [], transport_factory=tf)
        async with client_for(app) as client:
            resp = await client.get("/connections/health")
            assert resp.status_code == 200
            rows = resp.json()
            assert len(rows) == 1
            assert rows[0] == {
                "bundle": "svc",
                "connection": "crm",
                "status": "ok",
                "added": [],
                "removed": [],
                "schemaChanged": [],
            }


async def test_connections_health_drift_when_a_tool_is_added(tmp_path: Path) -> None:
    async with mock_mcp_server() as handle:
        tf = asgi_transport_factory(handle)
        _write_bundle(tmp_path, url=handle.url)
        # Snapshot only "echo" as the committed baseline; live server also
        # has "add", "big_result", "trigger_list_changed" -> drift (added).
        await refresh_snapshots(tmp_path, transport_factory=tf, captured_at="t")
        import json as _json

        snapshot_path = tmp_path / "shared" / "svc" / "snapshots" / "crm.json"
        data = _json.loads(snapshot_path.read_text())
        data["tools"] = [t for t in data["tools"] if t["name"] == "echo"]
        snapshot_path.write_text(_json.dumps(data))

        app = make_app(tmp_path, [], transport_factory=tf)
        async with client_for(app) as client:
            resp = await client.get("/connections/health")
            rows = resp.json()
            assert len(rows) == 1
            assert rows[0]["status"] == "drift"
            assert set(rows[0]["added"]) >= {"add", "big_result"}
            assert rows[0]["removed"] == []


async def test_connections_health_unreachable_does_not_fail_the_endpoint(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "shared/svc/bundle.yaml": (
                "connections:\n"
                "  crm:\n"
                "    url: http://127.0.0.1:1/does-not-exist\n"
                "    transport: streamable_http\n"
            ),
            "shared/svc/snapshots/crm.json": (
                '{"connection": "crm", "capturedAt": "t", "tools": []}\n'
            ),
            "agents/root/instructions.md": "you help with billing\n",
            "agents/root/agent.yaml": "use: [svc]\n",
        },
    )
    app = make_app(tmp_path, [])
    async with client_for(app) as client:
        resp = await client.get("/connections/health")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["bundle"] == "svc"
        assert rows[0]["connection"] == "crm"
        assert rows[0]["status"] == "unreachable"


async def test_connections_health_multiple_connections_run_concurrently(tmp_path: Path) -> None:
    async with mock_mcp_server() as handle_a, mock_mcp_server() as handle_b:
        tf_a = asgi_transport_factory(handle_a)
        tf_b = asgi_transport_factory(handle_b)
        write_files(
            tmp_path,
            {
                "shared/svc/bundle.yaml": (
                    "connections:\n"
                    f"  crm:\n    url: {handle_a.url}\n    transport: streamable_http\n"
                    f"  billing:\n    url: {handle_b.url}\n    transport: streamable_http\n"
                ),
                "agents/root/instructions.md": "hi\n",
                "agents/root/agent.yaml": "use: [svc]\n",
            },
        )

        # A single transport_factory that routes by matching whichever mock
        # app is reachable at the connection's own recorded URL: reuse
        # asgi_transport_factory per handle by dispatching on url prefix.
        def combined_transport_factory(url: str):
            if url.startswith(handle_a.url):
                return tf_a(url)
            return tf_b(url)

        await refresh_snapshots(
            tmp_path, transport_factory=combined_transport_factory, captured_at="t"
        )

        app = make_app(tmp_path, [], transport_factory=combined_transport_factory)
        async with client_for(app) as client:
            resp = await client.get("/connections/health")
            rows = {row["connection"]: row for row in resp.json()}
            assert rows.keys() == {"crm", "billing"}
            assert rows["crm"]["status"] == "ok"
            assert rows["billing"]["status"] == "ok"
