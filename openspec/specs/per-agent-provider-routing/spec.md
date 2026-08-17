# per-agent-provider-routing

## Purpose

Makes `agent.yaml`'s `model:` block fully load-bearing at serve time: `provider` selects which provider serves the agent's sessions, `max_tokens` is passed through on every call, and a per-agent thinking budget is configurable.

## Requirements

### Requirement: Per-agent provider selection
`knot serve` SHALL route each session's model requests through the provider named by that agent's `model.provider`, for parent sessions and delegated child sessions alike (each child using its own agent's configured provider). An agent that omits `model.provider` SHALL use the serving default provider.

#### Scenario: Two agents on different providers
- **WHEN** a fleet contains one agent configured with `provider: anthropic` and another with `provider: litellm`, and sessions are started for each
- **THEN** each session's requests go to its own agent's configured provider within a single `knot serve` process

#### Scenario: Unknown provider rejected at startup
- **WHEN** an agent names a provider that the serving process cannot construct (unknown name or missing credentials/configuration)
- **THEN** `knot serve` fails at startup with a diagnostic naming the agent and provider, rather than failing at first request

### Requirement: max_tokens honored per call
When an agent configures `model.max_tokens`, every provider request for that agent's sessions SHALL carry it as the response token ceiling, matching the documented "passed through to the provider on every call" behavior.

#### Scenario: Configured ceiling reaches the provider
- **WHEN** an agent sets `model.max_tokens: 2048` and a session for it makes a model request
- **THEN** the provider request carries a 2048 response-token ceiling

### Requirement: Per-agent thinking budget
An agent SHALL be able to configure an extended-thinking token budget in its `model:` block. When set and the provider supports it, requests enable thinking with that budget; when unset, thinking behavior is the provider's default. Setting it for a provider that does not support thinking SHALL be surfaced as a compile-time diagnostic, not a silent no-op.

#### Scenario: Thinking enabled for one agent
- **WHEN** one agent sets a thinking budget and another does not, both served by a thinking-capable provider
- **THEN** only the first agent's requests enable extended thinking

### Requirement: Backward compatibility for provider-less fleets
A fleet in which no agent sets `model.provider` SHALL behave exactly as under v0 single-provider serving: one default provider instance serves every session.

#### Scenario: Existing fleet unchanged
- **WHEN** a fleet compiled before this change (no `model.provider` set anywhere) is served
- **THEN** all sessions use the serving default provider and no new configuration is required
