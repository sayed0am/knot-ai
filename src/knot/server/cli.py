"""``knot`` command-line entry point.

A thin ``argparse`` dispatcher over subcommands: ``export``, ``validate``,
``refresh``, and ``serve``. The ``build_parser``/subcommand-handler
structure is set up so later subcommands can be added as additional
``subparsers.add_parser(...)`` blocks without touching ``main``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from knot.authoring.compile import compile_fleet
from knot.authoring.connections import format_refresh_report, refresh_snapshots
from knot.authoring.validate import format_report, run_validate
from knot.core.session import SessionStore, export_session_jsonl
from knot.server.app import create_app


def _cmd_export(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    try:
        if store.get_session(args.session_id) is None:
            print(f"knot: no such session {args.session_id!r} in {args.db}", file=sys.stderr)
            return 1
        out = Path(args.output) if args.output else sys.stdout
        export_session_jsonl(store, args.session_id, out)
    finally:
        store.close()
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    if args.check and not args.manifests:
        print("knot: --check requires --manifests", file=sys.stderr)
        return 2
    manifests_dir = Path(args.manifests) if args.manifests else None
    result = run_validate(
        Path(args.root), manifests_dir=manifests_dir, write=args.write, check=args.check
    )
    print(format_report(result))
    return result.exit_code


def _cmd_refresh(args: argparse.Namespace) -> int:
    try:
        report = asyncio.run(
            refresh_snapshots(Path(args.root), bundle_id=args.bundle, connection=args.connection)
        )
    except ValueError as exc:
        print(f"knot: {exc}", file=sys.stderr)
        return 1
    print(format_refresh_report(report))
    return 0 if report.ok else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    root = Path(args.root)
    fleet = compile_fleet(root)
    failed = sorted(agent_id for agent_id, compiled in fleet.agents.items() if not compiled.ok)
    if failed and not args.allow_broken:
        print(
            f"knot: refusing to start: {len(failed)} agent(s) failed to compile: "
            f"{', '.join(failed)} (pass --allow-broken to start anyway)",
            file=sys.stderr,
        )
        return 1

    # v0.1 provider policy: 'serve' talks to every session, parent and
    # descendant subagent alike, through one Anthropic provider instance
    # (an agent's own manifest 'model.name' still selects which model that
    # provider is asked for). Per-manifest provider choice (openai,
    # openrouter, litellm, ...) is a later work package.
    from knot.providers.anthropic import AnthropicProvider

    store = SessionStore(args.db)
    provider = AnthropicProvider()
    app = create_app(fleet=fleet, store=store, provider=provider)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="knot", description="knot agent framework CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser(
        "export", help="Export a session's durable entry log as JSONL"
    )
    export_parser.add_argument("session_id", help="Session id to export")
    export_parser.add_argument("--db", required=True, help="Path to the session SQLite database")
    export_parser.add_argument("-o", "--output", help="Output file (default: stdout)")
    export_parser.set_defaults(handler=_cmd_export)

    validate_parser = subparsers.add_parser(
        "validate", help="Compile every agent in a fleet and report diagnostics"
    )
    validate_parser.add_argument("--root", required=True, help="Path to the fleet root directory")
    validate_parser.add_argument(
        "--manifests", help="Directory of committed manifest JSON files to diff against"
    )
    validate_parser.add_argument(
        "--write", action="store_true", help="Write/update manifest files in --manifests"
    )
    validate_parser.add_argument(
        "--check",
        action="store_true",
        help="Exit with status 2 if manifests in --manifests are stale",
    )
    validate_parser.set_defaults(handler=_cmd_validate)

    refresh_parser = subparsers.add_parser(
        "refresh", help="Refresh committed MCP connection snapshots against their live servers"
    )
    refresh_parser.add_argument("--root", required=True, help="Path to the fleet root directory")
    refresh_parser.add_argument("--bundle", help="Only refresh connections in this bundle")
    refresh_parser.add_argument(
        "--connection", help="Only refresh this connection (requires --bundle)"
    )
    refresh_parser.set_defaults(handler=_cmd_refresh)

    serve_parser = subparsers.add_parser("serve", help="Serve a compiled fleet over HTTP")
    serve_parser.add_argument("--root", required=True, help="Path to the fleet root directory")
    serve_parser.add_argument("--db", required=True, help="Path to the session SQLite database")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    serve_parser.add_argument(
        "--allow-broken",
        action="store_true",
        help="Start even if one or more agents failed to compile",
    )
    serve_parser.set_defaults(handler=_cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
