# model-visible-logged-invariant

## Purpose

Guarantees restart-resume equivalence for persisted sessions by asserting, at the model-request boundary, that the history the model is about to see is exactly what the durable entry log reconstructs.

## Requirements

### Requirement: Invariant checked before every provider request

For a session with persistence attached, before each provider request the system SHALL verify that the provider-visible history derived from the durable entry log equals the in-memory history being sent, compared under the same canonical projection the provider request is built from.

#### Scenario: Divergence caught before the provider call

- **WHEN** an in-memory message was added to a persisted session's history without a corresponding durable entry, and the next provider request is about to be made
- **THEN** the divergence is detected before the provider is called

#### Scenario: Equivalent histories pass silently

- **WHEN** the log-derived history and the in-memory history project to the same canonical form
- **THEN** the request proceeds with no observable effect from the check

### Requirement: Enforcement modes

The check SHALL support two enforcement modes: `strict` (the run ends with an error outcome before the provider call, naming the divergence) and `warn` (the divergence is logged and the run continues). The mode SHALL be configurable at the server/runtime level. Test and validation harnesses SHALL default to `strict`; the serving default SHALL be `warn`.

#### Scenario: Strict mode fails the run

- **WHEN** a divergence is detected in strict mode
- **THEN** the run ends with an error outcome before any provider request is made, and the error identifies the check

#### Scenario: Warn mode continues

- **WHEN** a divergence is detected in warn mode
- **THEN** a warning carrying the divergence report is emitted and the provider request proceeds with the in-memory history

### Requirement: Scope is persisted sessions only

A harness with no persistence attached SHALL be exempt: there is no durable log to diverge from, and no check runs.

#### Scenario: Bare in-memory harness

- **WHEN** an `AgentHarness` runs without a session store subscriber
- **THEN** no invariant check occurs and behavior is unchanged

### Requirement: Divergence report

A detected divergence SHALL be reported with enough detail to debug it: the position of the first diverging message (index and, where applicable, entry seq), which side has the extra or differing message, and a field-level description of the difference.

#### Scenario: Report pinpoints the first divergence

- **WHEN** the log-derived history and in-memory history first differ at position N
- **THEN** the report names position N, the entry seq when one exists, and the differing fields — not merely that "histories differ"

### Requirement: Canonical projection tolerates provider-invisible differences

The comparison SHALL ignore differences that cannot reach the provider (fields excluded from provider payloads, such as tool-result `details` and local timestamps). A difference only in provider-invisible fields SHALL NOT count as divergence.

#### Scenario: Details-only difference passes

- **WHEN** a tool result's `details` payload differs between the rehydrated and in-memory copies but its content, role, and tool linkage are identical
- **THEN** the check passes

### Requirement: The invariant is a stated design rule

The invariant SHALL be documented as a named contract for future changes: any new model-visible input requires a durable entry (a new entry type or an extension of an existing one), never an in-memory-only injection.

#### Scenario: New feature adds model-visible input

- **WHEN** a future change introduces content the model will see (e.g. an injected advisory message)
- **THEN** that content is persisted as a durable entry, and the invariant check passes for sessions using the feature
