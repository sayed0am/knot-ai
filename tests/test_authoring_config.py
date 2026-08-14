"""agent.yaml / bundle.yaml schemas: strict parsing that never raises."""

from __future__ import annotations

from pathlib import Path

from authoring_fixtures import write_files

from knot.authoring.config import load_agent_config, load_bundle_config


def test_valid_agent_yaml_parses(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agent.yaml": """
                description: A helpful agent.
                model:
                  provider: anthropic
                  name: some-model
                  max_tokens: 4096
                limits:
                  max_turns: 20
                  delegation_max_per_turn: 2
                use: [utils]
                approvals:
                  send_email: always
            """
        },
    )

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert diagnostics == []
    assert config is not None
    assert config.description == "A helpful agent."
    assert config.model is not None
    assert config.model.provider == "anthropic"
    assert config.model.name == "some-model"
    assert config.model.max_tokens == 4096
    assert config.limits.max_turns == 20
    assert config.limits.delegation_max_per_turn == 2
    assert config.limits.delegation_max_concurrent == 2  # default preserved
    assert config.use == ["utils"]
    assert config.approvals == {"send_email": "always"}


def test_bare_agent_yaml_uses_all_defaults(tmp_path: Path) -> None:
    write_files(tmp_path, {"agent.yaml": "description: minimal\n"})

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert diagnostics == []
    assert config is not None
    assert config.model is None
    assert config.limits.max_turns is None
    assert config.use == []
    assert config.approvals == {}


def test_agent_yaml_typo_key_is_a_diagnostic(tmp_path: Path) -> None:
    write_files(tmp_path, {"agent.yaml": "modle: oops\n"})

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert config is None
    assert len(diagnostics) == 1
    assert diagnostics[0].severity == "error"
    assert diagnostics[0].agent_id == "helper"
    assert "modle" in diagnostics[0].message


def test_agent_yaml_invalid_yaml_is_a_diagnostic(tmp_path: Path) -> None:
    write_files(tmp_path, {"agent.yaml": "description: [unterminated\n"})

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert config is None
    assert len(diagnostics) == 1
    assert diagnostics[0].severity == "error"
    assert "YAML" in diagnostics[0].message


def test_valid_bundle_yaml_parses(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "bundle.yaml": """
                description: shared utilities
                approvals:
                  charge_card: once
                connections:
                  billing:
                    url: https://billing.example.internal/mcp
                    transport: streamable_http
                    allow: [get_invoice]
                    auth_env: BILLING_TOKEN
            """
        },
    )

    config, diagnostics = load_bundle_config(tmp_path / "bundle.yaml", bundle_id="utils")

    assert diagnostics == []
    assert config is not None
    assert config.description == "shared utilities"
    assert config.approvals == {"charge_card": "once"}
    connection = config.connections["billing"]
    assert connection.url == "https://billing.example.internal/mcp"
    assert connection.transport == "streamable_http"
    assert connection.allow == ["get_invoice"]
    assert connection.auth_env == "BILLING_TOKEN"


def test_bundle_yaml_typo_key_is_a_diagnostic(tmp_path: Path) -> None:
    write_files(tmp_path, {"bundle.yaml": "descriptoin: oops\n"})

    config, diagnostics = load_bundle_config(tmp_path / "bundle.yaml", bundle_id="utils")

    assert config is None
    assert len(diagnostics) == 1
    assert diagnostics[0].bundle_id == "utils"
    assert "descriptoin" in diagnostics[0].message


def test_stdio_transport_fails_validation_with_http_wrapping_guidance(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "bundle.yaml": """
                connections:
                  legacy:
                    url: stdio://some-command
                    transport: stdio
            """
        },
    )

    config, diagnostics = load_bundle_config(tmp_path / "bundle.yaml", bundle_id="utils")

    assert config is None
    assert len(diagnostics) == 1
    message = diagnostics[0].message
    assert "stdio" in message
    assert "HTTP" in message
    assert "wrap" in message.lower()


def test_allow_entry_with_dunder_qualification_is_rejected(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "bundle.yaml": """
                connections:
                  billing:
                    url: https://billing.example.internal/mcp
                    transport: streamable_http
                    allow: [billing__get_invoice]
            """
        },
    )

    config, diagnostics = load_bundle_config(tmp_path / "bundle.yaml", bundle_id="utils")

    assert config is None
    assert len(diagnostics) == 1
    assert "__" in diagnostics[0].message
    assert "billing__get_invoice" in diagnostics[0].message


def test_sse_transport_is_a_supported_http_transport(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "bundle.yaml": """
                connections:
                  events:
                    url: https://events.example.internal/mcp
                    transport: sse
            """
        },
    )

    config, diagnostics = load_bundle_config(tmp_path / "bundle.yaml", bundle_id="utils")

    assert diagnostics == []
    assert config is not None
    assert config.connections["events"].transport == "sse"
    assert config.connections["events"].allow is None
