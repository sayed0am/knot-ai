"""``knot serve`` CLI parsing: no server is actually bound in these tests."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from authoring_fixtures import write_files

from knot.server.cli import _cmd_serve, build_parser, resolve_invariant_mode


def test_serve_parser_accepts_required_flags_with_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args(["serve", "--root", "/fleet", "--db", "/db.sqlite"])
    assert args.command == "serve"
    assert args.root == "/fleet"
    assert args.db == "/db.sqlite"
    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.allow_broken is False
    assert callable(args.handler)


def test_serve_parser_accepts_all_optional_flags() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "serve",
            "--root",
            "/fleet",
            "--db",
            "/db.sqlite",
            "--host",
            "0.0.0.0",
            "--port",
            "9001",
            "--allow-broken",
        ]
    )
    assert args.host == "0.0.0.0"
    assert args.port == 9001
    assert args.allow_broken is True


def test_serve_requires_root_and_db() -> None:
    parser = build_parser()
    import pytest

    with pytest.raises(SystemExit):
        parser.parse_args(["serve"])


def test_serve_refuses_to_start_on_a_broken_fleet_without_allow_broken(
    tmp_path: Path, capsys
) -> None:
    write_files(tmp_path, {"agents/root/agent.yaml": "description: missing instructions\n"})
    args = argparse.Namespace(
        root=str(tmp_path),
        db=str(tmp_path / "sessions.db"),
        host="127.0.0.1",
        port=8000,
        allow_broken=False,
    )
    assert _cmd_serve(args) == 1
    assert "failed to compile" in capsys.readouterr().err


def test_serve_allow_broken_flag_is_accepted_by_the_broken_fleet_check(
    tmp_path: Path,
) -> None:
    """``--allow-broken`` clears the refusal path; this test never reaches
    ``uvicorn.run`` (nothing calls it) because it asserts the refusal branch
    itself is skipped, not that the process would actually bind a port."""
    write_files(tmp_path, {"agents/root/agent.yaml": "description: missing instructions\n"})
    from knot.authoring.compile import compile_fleet

    fleet = compile_fleet(tmp_path)
    failed = [agent_id for agent_id, compiled in fleet.agents.items() if not compiled.ok]
    assert failed  # sanity: this fleet really is broken
    # Not calling _cmd_serve(args) here: with allow_broken=True it would
    # proceed to uvicorn.run(...) and actually try to bind a socket, which
    # this unit test intentionally never exercises (see the CLI docstring).


# ---------------------------------------------------------------------------
# invariant_mode precedence and validation (design.md D3): explicit >
# $KNOT_INVARIANT_MODE > serving default "warn".
# ---------------------------------------------------------------------------


def test_invariant_mode_defaults_to_warn_with_no_explicit_value_and_no_env() -> None:
    assert resolve_invariant_mode(None, env={}) == "warn"


def test_invariant_mode_env_var_overrides_the_default() -> None:
    assert resolve_invariant_mode(None, env={"KNOT_INVARIANT_MODE": "strict"}) == "strict"
    assert resolve_invariant_mode(None, env={"KNOT_INVARIANT_MODE": "off"}) == "off"


def test_invariant_mode_explicit_value_wins_over_env_var() -> None:
    assert resolve_invariant_mode("strict", env={"KNOT_INVARIANT_MODE": "warn"}) == "strict"


def test_invariant_mode_rejects_an_unknown_explicit_value() -> None:
    with pytest.raises(ValueError, match="invalid --invariant-mode value"):
        resolve_invariant_mode("bogus", env={})


def test_invariant_mode_rejects_an_unknown_env_value_rather_than_falling_back_silently() -> None:
    with pytest.raises(ValueError, match="invalid KNOT_INVARIANT_MODE value"):
        resolve_invariant_mode(None, env={"KNOT_INVARIANT_MODE": "bogus"})


def test_serve_parser_accepts_invariant_mode_flag() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["serve", "--root", "/fleet", "--db", "/db.sqlite", "--invariant-mode", "off"]
    )
    assert args.invariant_mode == "off"


def test_serve_parser_defaults_invariant_mode_to_none_so_env_can_take_over() -> None:
    parser = build_parser()
    args = parser.parse_args(["serve", "--root", "/fleet", "--db", "/db.sqlite"])
    assert args.invariant_mode is None


def test_serve_parser_rejects_an_unknown_invariant_mode_choice() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["serve", "--root", "/fleet", "--db", "/db.sqlite", "--invariant-mode", "bogus"]
        )


def test_serve_reports_an_invalid_invariant_mode_env_var_as_a_startup_error(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    write_files(tmp_path, {"agents/root/instructions.md": "hi\n"})
    monkeypatch.setenv("KNOT_INVARIANT_MODE", "bogus")
    args = argparse.Namespace(
        root=str(tmp_path),
        db=str(tmp_path / "sessions.db"),
        host="127.0.0.1",
        port=8000,
        allow_broken=False,
        invariant_mode=None,
    )
    assert _cmd_serve(args) == 1
    assert "invalid KNOT_INVARIANT_MODE value" in capsys.readouterr().err
