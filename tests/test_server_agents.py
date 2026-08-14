"""``GET /agents`` and ``GET /agents/{id}``: fleet listing and manifest serving."""

from __future__ import annotations

import json
from pathlib import Path

from authoring_fixtures import write_files
from server_fixtures import client_for, make_app

from knot.authoring.compile import compile_fleet
from knot.authoring.manifest import serialize_manifest


async def test_list_agents_includes_ok_and_failed_with_descriptions(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/root/instructions.md": "you are root\n",
            "agents/root/agent.yaml": "description: The root agent.\n",
            # broken: agent.yaml present (readable description) but
            # instructions.md missing -> fails compile.
            "agents/broken/agent.yaml": "description: A broken one.\n",
        },
    )
    app = make_app(tmp_path, [])

    async with client_for(app) as client:
        resp = await client.get("/agents")
        assert resp.status_code == 200
        rows = {row["agentId"]: row for row in resp.json()}
        assert rows.keys() == {"root", "broken"}
        assert rows["root"]["ok"] is True
        assert rows["root"]["description"] == "The root agent."
        assert rows["broken"]["ok"] is False
        assert rows["broken"]["description"] == "A broken one."


async def test_list_agents_description_is_none_without_agent_yaml(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "you are root\n"})
    app = make_app(tmp_path, [])

    async with client_for(app) as client:
        resp = await client.get("/agents")
        assert resp.json() == [{"agentId": "root", "ok": True, "description": None}]


async def test_get_agent_serves_the_compiled_manifest_verbatim(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agents/root/instructions.md": "you are root\n",
            "agents/root/agent.yaml": "description: The root agent.\n",
        },
    )
    fleet = compile_fleet(tmp_path)
    expected = serialize_manifest(fleet.agents["root"].manifest)

    app = make_app(tmp_path, [])
    async with client_for(app) as client:
        resp = await client.get("/agents/root")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.text == expected
        assert json.loads(resp.text)["agentId"] == "root"


async def test_get_agent_unknown_is_404(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "you are root\n"})
    app = make_app(tmp_path, [])
    async with client_for(app) as client:
        resp = await client.get("/agents/nope")
        assert resp.status_code == 404


async def test_get_agent_failed_compile_is_409_with_diagnostics(tmp_path: Path) -> None:
    write_files(tmp_path, {"agents/broken/agent.yaml": "description: no instructions\n"})
    app = make_app(tmp_path, [])
    async with client_for(app) as client:
        resp = await client.get("/agents/broken")
        assert resp.status_code == 409
        body = resp.json()
        assert "diagnostics" in body
        assert len(body["diagnostics"]) >= 1
        diag = body["diagnostics"][0]
        assert diag["severity"] == "error"
        assert "instructions.md" in diag["message"]
        assert diag["agentId"] == "broken"
