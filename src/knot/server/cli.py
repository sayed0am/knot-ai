"""``knot`` command-line entry point.

A thin ``argparse`` dispatcher over subcommands: ``export``, ``validate``,
``refresh``, and ``serve``. The ``build_parser``/subcommand-handler
structure is set up so later subcommands can be added as additional
``subparsers.add_parser(...)`` blocks without touching ``main``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from knot.authoring.compile import CompiledAgent, CompiledFleet, compile_fleet
from knot.authoring.connections import format_refresh_report, refresh_snapshots
from knot.authoring.validate import format_report, run_validate
from knot.core.invariant import InvariantMode
from knot.core.session import SessionStore, export_session_jsonl
from knot.providers.provider import ModelProvider
from knot.server.app import create_app

#: See ``resolve_invariant_mode``. Named for the setting it overrides, not
#: for "knot" generically, since more env-overridable settings may follow.
INVARIANT_MODE_ENV_VAR = "KNOT_INVARIANT_MODE"

#: See ``resolve_serve_token`` (design.md D9).
SERVE_TOKEN_ENV_VAR = "KNOT_SERVE_TOKEN"

_INVARIANT_MODES: tuple[InvariantMode, ...] = ("strict", "warn", "off")

#: The serving default (see design.md D3): a false positive in production
#: must degrade to telemetry, not an outage. Test fixtures pass "strict"
#: explicitly instead of relying on this constant — it names only the
#: ``knot serve`` default.
DEFAULT_SERVING_INVARIANT_MODE: InvariantMode = "warn"

#: Every provider name ``knot serve`` can construct — must match
#: ``knot.authoring.config.ModelConfig.provider``'s ``Literal`` exactly
#: (design.md D2: "the registry must cover exactly that set"). Keep the two
#: in sync if either changes.
SUPPORTED_PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "openrouter", "litellm")

#: The serving default (design.md D2): constructed unconditionally, even if
#: no agent's manifest names it, matching the pre-D2 behavior of always
#: starting one ``AnthropicProvider`` regardless of fleet content.
DEFAULT_SERVING_PROVIDER = "anthropic"


class ProviderStartupError(Exception):
    """A compiled fleet names a provider ``knot serve`` cannot construct —
    an unknown name or a failed construction (missing credentials, a
    missing optional dependency, ...). Raised by ``build_provider_registry``
    so ``_cmd_serve`` can report a diagnostic naming the agent(s) and
    provider and exit non-zero, rather than failing at first request
    (design.md D2's "Unknown provider rejected at startup")."""


def _referenced_providers(fleet: CompiledFleet) -> dict[str, list[str]]:
    """Every distinct ``model.provider`` value named by a successfully
    compiled agent anywhere in ``fleet`` — top-level and every nested
    subagent — mapped to the agent id(s) that name it, plus
    ``DEFAULT_SERVING_PROVIDER`` (mapped to an empty list: it is
    constructed unconditionally, not because some agent referenced it).
    A failed (``ok=False``) agent has no manifest to read a provider off
    of and is skipped, matching every other manifest-reading pass over a
    fleet.
    """
    referenced: dict[str, list[str]] = {DEFAULT_SERVING_PROVIDER: []}

    def walk(agents: Mapping[str, CompiledAgent]) -> None:
        for compiled in agents.values():
            if not compiled.ok:
                continue
            manifest = compiled.manifest
            assert manifest is not None  # ok=True always has a manifest
            if manifest.model is not None:
                referenced.setdefault(manifest.model.provider, []).append(compiled.agent_id)
            walk(compiled.subagents)

    walk(fleet.agents)
    return referenced


def _construct_provider(name: str) -> ModelProvider:
    """Build one named provider adapter from its own existing env-var
    configuration convention (design.md D2). Imports are local so
    constructing one provider never pulls in another's dependencies (in
    particular, the optional ``litellm`` package stays uninstalled-safe
    unless a fleet actually names ``litellm``).
    """
    if name == "anthropic":
        from knot.providers.anthropic import AnthropicProvider  # noqa: PLC0415

        return AnthropicProvider()
    if name == "openai":
        from knot.providers.openai_compatible import openai_provider  # noqa: PLC0415

        return openai_provider()
    if name == "openrouter":
        from knot.providers.openai_compatible import openrouter_provider  # noqa: PLC0415

        return openrouter_provider()
    if name == "litellm":
        from knot.providers.litellm import LiteLLMProvider  # noqa: PLC0415

        return LiteLLMProvider()
    raise ValueError(
        f"unknown provider {name!r}: supported providers are {', '.join(SUPPORTED_PROVIDERS)}"
    )


def build_provider_registry(fleet: CompiledFleet) -> dict[str, ModelProvider]:
    """Construct exactly the provider adapters ``fleet`` references (plus
    the serving default), or raise ``ProviderStartupError`` naming the
    offending agent(s) and provider — design.md D2's registry, built only
    once at ``knot serve`` startup rather than per-request.
    """
    registry: dict[str, ModelProvider] = {}
    for name, agent_ids in _referenced_providers(fleet).items():
        try:
            registry[name] = _construct_provider(name)
        except Exception as exc:
            agents = ", ".join(agent_ids) if agent_ids else "(serving default)"
            raise ProviderStartupError(
                f"agent(s) {agents}: could not start provider {name!r}: {exc}"
            ) from exc
    return registry


def resolve_invariant_mode(
    explicit: str | None, *, env: Mapping[str, str] | None = None
) -> InvariantMode:
    """Resolve the effective ``invariant_mode`` for ``knot serve``.

    Precedence: an explicit value (the ``--invariant-mode`` flag) wins
    outright over ``$KNOT_INVARIANT_MODE``, which in turn wins over the
    serving default ``"warn"``. An unrecognized value from either source is
    a startup ``ValueError`` — never a silent fallback to the default, since
    that would quietly demote a deliberately-configured safety check (e.g. a
    typo'd env var meant to set "strict") to warn without telling anyone.
    """
    resolved_env = env if env is not None else os.environ
    candidate = explicit if explicit is not None else resolved_env.get(INVARIANT_MODE_ENV_VAR)
    if candidate is None:
        return DEFAULT_SERVING_INVARIANT_MODE
    if candidate not in _INVARIANT_MODES:
        source = "--invariant-mode" if explicit is not None else INVARIANT_MODE_ENV_VAR
        raise ValueError(
            f"invalid {source} value {candidate!r}: must be one of {', '.join(_INVARIANT_MODES)}"
        )
    return candidate  # type: ignore[return-value]  # validated against _INVARIANT_MODES above


def resolve_serve_token(
    explicit: str | None, *, env: Mapping[str, str] | None = None
) -> str | None:
    """Resolve the effective bearer token for ``knot serve`` (design.md D9).

    Precedence: an explicit value (the ``--token`` flag) wins outright over
    ``$KNOT_SERVE_TOKEN``, which in turn wins over "no token" (``None``) —
    the same precedence convention as ``resolve_invariant_mode``, minus that
    function's "unknown value" validation: unlike an invariant mode, any
    non-empty string is a valid token, so there is nothing to reject.
    ``None`` means auth stays disabled — ``create_app``'s default, zero
    behavior change from v0.
    """
    resolved_env = env if env is not None else os.environ
    return explicit if explicit is not None else resolved_env.get(SERVE_TOKEN_ENV_VAR)


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

    try:
        invariant_mode = resolve_invariant_mode(args.invariant_mode)
    except ValueError as exc:
        print(f"knot: {exc}", file=sys.stderr)
        return 1

    # Per-agent provider routing (design.md D2): each session is served by
    # the provider its own agent's manifest names (falling back to
    # DEFAULT_SERVING_PROVIDER when unset) — an agent's manifest
    # 'model.name' still selects which model that provider is asked for.
    # Only the providers the fleet actually references (plus the serving
    # default) are constructed, and construction failure — an unknown name
    # or missing credentials — fails startup with a named diagnostic rather
    # than the first request that needs it.
    try:
        providers = build_provider_registry(fleet)
    except ProviderStartupError as exc:
        print(f"knot: {exc}", file=sys.stderr)
        return 1

    # design.md D9: explicit --token wins over $KNOT_SERVE_TOKEN; neither
    # present leaves auth disabled, exactly as before this flag existed.
    # Never logged or echoed anywhere below — see create_app's own docstring.
    auth_token = resolve_serve_token(args.token)

    store = SessionStore(args.db)
    app = create_app(
        fleet=fleet,
        store=store,
        providers=providers,
        default_provider=DEFAULT_SERVING_PROVIDER,
        runtime_kwargs={"invariant_mode": invariant_mode},
        auth_token=auth_token,
    )

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
    serve_parser.add_argument(
        "--invariant-mode",
        choices=_INVARIANT_MODES,
        default=None,
        help=(
            "Model-visible-logged invariant enforcement: 'strict' fails a run on "
            "divergence, 'warn' logs and continues, 'off' disables the check. "
            f"Defaults to ${INVARIANT_MODE_ENV_VAR} if set, else 'warn'."
        ),
    )
    serve_parser.add_argument(
        "--token",
        default=None,
        help=(
            "Static bearer token required on every request (design D9). "
            f"Defaults to ${SERVE_TOKEN_ENV_VAR} if set, else no auth is enforced."
        ),
    )
    serve_parser.set_defaults(handler=_cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_SERVING_PROVIDER",
    "SUPPORTED_PROVIDERS",
    "ProviderStartupError",
    "build_parser",
    "build_provider_registry",
    "main",
    "resolve_invariant_mode",
    "resolve_serve_token",
]
