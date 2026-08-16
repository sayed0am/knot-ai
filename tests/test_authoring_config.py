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


def test_model_context_window_parses(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agent.yaml": """
                model:
                  provider: anthropic
                  name: some-model
                  context_window: 200000
            """
        },
    )

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert diagnostics == []
    assert config is not None
    assert config.model is not None
    assert config.model.context_window == 200_000


def test_model_context_window_defaults_to_none(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agent.yaml": """
                model:
                  provider: anthropic
                  name: some-model
            """
        },
    )

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert diagnostics == []
    assert config is not None
    assert config.model is not None
    assert config.model.context_window is None


def test_model_context_window_zero_is_rejected(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agent.yaml": """
                model:
                  provider: anthropic
                  name: some-model
                  context_window: 0
            """
        },
    )

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert config is None
    assert len(diagnostics) == 1
    assert diagnostics[0].severity == "error"
    assert "context_window" in diagnostics[0].message


def test_compaction_block_defaults_when_absent(tmp_path: Path) -> None:
    write_files(tmp_path, {"agent.yaml": "description: minimal\n"})

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert diagnostics == []
    assert config is not None
    assert config.compaction.enabled is True
    assert config.compaction.threshold_ratio == 0.8
    assert config.compaction.retain_budget == 0.16
    assert config.compaction.summarization_model is None
    assert config.compaction.max_overflow_retries == 1
    assert config.compaction.summarization_max_tokens == 8192


def test_compaction_block_parses_explicit_values(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agent.yaml": """
                compaction:
                  enabled: false
                  threshold_ratio: 0.7
                  retain_budget: 0.1
                  summarization_model: some-small-model
                  max_overflow_retries: 3
                  summarization_max_tokens: 2048
            """
        },
    )

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert diagnostics == []
    assert config is not None
    assert config.compaction.enabled is False
    assert config.compaction.threshold_ratio == 0.7
    assert config.compaction.retain_budget == 0.1
    assert config.compaction.summarization_model == "some-small-model"
    assert config.compaction.max_overflow_retries == 3
    assert config.compaction.summarization_max_tokens == 2048


def test_compaction_threshold_ratio_out_of_range_is_rejected(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agent.yaml": """
                compaction:
                  threshold_ratio: 1.5
            """
        },
    )

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert config is None
    assert len(diagnostics) == 1
    assert diagnostics[0].severity == "error"
    assert "threshold_ratio" in diagnostics[0].message


def test_compaction_threshold_ratio_zero_is_rejected(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agent.yaml": """
                compaction:
                  threshold_ratio: 0
            """
        },
    )

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert config is None
    assert "threshold_ratio" in diagnostics[0].message


def test_compaction_retain_budget_out_of_range_is_rejected(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agent.yaml": """
                compaction:
                  retain_budget: 1.0
            """
        },
    )

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert config is None
    assert len(diagnostics) == 1
    assert "retain_budget" in diagnostics[0].message


def test_compaction_retain_budget_must_be_below_threshold_ratio(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agent.yaml": """
                compaction:
                  threshold_ratio: 0.3
                  retain_budget: 0.5
            """
        },
    )

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert config is None
    assert len(diagnostics) == 1
    assert "retain_budget" in diagnostics[0].message
    assert "threshold_ratio" in diagnostics[0].message


def test_compaction_retain_budget_equal_to_threshold_ratio_is_rejected(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agent.yaml": """
                compaction:
                  threshold_ratio: 0.4
                  retain_budget: 0.4
            """
        },
    )

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert config is None


def test_compaction_max_overflow_retries_negative_is_rejected(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agent.yaml": """
                compaction:
                  max_overflow_retries: -1
            """
        },
    )

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert config is None
    assert "max_overflow_retries" in diagnostics[0].message


def test_compaction_max_overflow_retries_non_numeric_is_rejected(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agent.yaml": """
                compaction:
                  max_overflow_retries: not-a-number
            """
        },
    )

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert config is None
    assert len(diagnostics) == 1
    assert diagnostics[0].severity == "error"
    assert "max_overflow_retries" in diagnostics[0].message


def test_compaction_summarization_max_tokens_zero_is_rejected(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agent.yaml": """
                compaction:
                  summarization_max_tokens: 0
            """
        },
    )

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert config is None
    assert "summarization_max_tokens" in diagnostics[0].message


def test_compaction_unknown_key_is_rejected(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "agent.yaml": """
                compaction:
                  not_a_real_field: true
            """
        },
    )

    config, diagnostics = load_agent_config(tmp_path / "agent.yaml", agent_id="helper")

    assert config is None
    assert len(diagnostics) == 1
    assert diagnostics[0].severity == "error"


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
