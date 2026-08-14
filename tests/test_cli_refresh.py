"""``knot refresh`` CLI smoke test, against a real (if ephemeral) MCP server.

Every other test in this package points the client at an in-process ASGI
app (see ``tests/mcp_fixtures.py``) — the right choice when the *test* owns
the event loop. Here the CLI's own ``_cmd_refresh`` calls ``asyncio.run()``
internally, so this test cannot itself be a coroutine (nesting
``asyncio.run()`` inside a running loop fails); instead it runs a real
``uvicorn`` server on an ephemeral localhost port in a background thread —
still fully offline (loopback only, no hardcoded port) — and drives the CLI
exactly as a user would from a shell.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import uvicorn
from authoring_fixtures import write_files
from mcp_fixtures import build_mock_server

from knot.server.cli import main


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _serve_in_background(port: int) -> uvicorn.Server:
    from mcp.server.transport_security import TransportSecuritySettings

    server = build_mock_server()
    app = server.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if uv_server.started:
            return uv_server
        time.sleep(0.01)
    raise RuntimeError("mock uvicorn server did not start in time")


def test_cli_refresh_writes_snapshot_and_exits_zero(tmp_path: Path, capsys) -> None:
    port = _free_port()
    uv_server = _serve_in_background(port)
    try:
        write_files(
            tmp_path,
            {
                "shared/svc/bundle.yaml": (
                    "connections:\n"
                    "  api:\n"
                    f"    url: http://127.0.0.1:{port}/mcp\n"
                    "    transport: streamable_http\n"
                ),
                "agents/helper/instructions.md": "hi\n",
                "agents/helper/agent.yaml": "use: [svc]\n",
            },
        )

        exit_code = main(["refresh", "--root", str(tmp_path)])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "svc/api" in captured.out
        assert "1 connection(s) refreshed, 0 unreachable" in captured.out

        snapshot_path = tmp_path / "shared" / "svc" / "snapshots" / "api.json"
        assert snapshot_path.is_file()
        assert '"echo"' in snapshot_path.read_text()
    finally:
        uv_server.should_exit = True


def test_cli_refresh_unreachable_connection_exits_nonzero(tmp_path: Path, capsys) -> None:
    write_files(
        tmp_path,
        {
            "shared/svc/bundle.yaml": (
                "connections:\n"
                "  api:\n"
                "    url: http://127.0.0.1:1/mcp\n"  # nothing listens on port 1
                "    transport: streamable_http\n"
            ),
            "agents/helper/instructions.md": "hi\n",
            "agents/helper/agent.yaml": "use: [svc]\n",
        },
    )

    exit_code = main(["refresh", "--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "UNREACHABLE" in captured.out


def test_cli_refresh_unknown_bundle_is_a_clean_error(tmp_path: Path, capsys) -> None:
    exit_code = main(["refresh", "--root", str(tmp_path), "--bundle", "nope"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "nope" in captured.err
