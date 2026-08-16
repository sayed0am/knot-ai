## Purpose

Keeps long-lived sessions within the model's context window by durably replacing the oldest span of conversation history with a model-generated summary, while preserving the append-only entry log as a complete audit trail.

## ADDED Requirements

### Requirement: Compaction is a durable, append-only fact

A compaction SHALL be recorded as a new entry appended to the session's entry log, carrying the summary and the boundary of the history span it replaces. Existing entries SHALL NOT be deleted or rewritten by compaction. Rehydrating a compacted session SHALL derive exactly the post-compaction history the live session saw.

#### Scenario: Restart after compaction

- **WHEN** a session that has undergone compaction is rehydrated after a process restart
- **THEN** the derived message history equals the live session's post-compaction history (summary message plus retained tail), and the next provider request is built from that same history

#### Scenario: Audit trail preserved

- **WHEN** a session's raw entries are read (export, fleet queries) after compaction
- **THEN** every pre-compaction message entry is still present in the log in its original form, alongside the compaction entry

### Requirement: Proactive compaction at the turn boundary

When the provider-reported context consumption of the most recent assistant response exceeds a configured threshold ratio of the model's known context capacity, the session SHALL compact before the next provider request is made. When the model's context capacity is not known, proactive compaction SHALL be skipped (reactive recovery still applies).

#### Scenario: Threshold crossed between turns

- **WHEN** the latest assistant response reports input-token usage at or above `threshold_ratio × context_window` and another provider request is about to be made
- **THEN** compaction runs first, and the next provider request is built from the compacted history

#### Scenario: Unknown capacity

- **WHEN** no context capacity is configured or derivable for the session's model
- **THEN** no proactive compaction occurs and the run proceeds normally

### Requirement: Reactive recovery from context overflow

When a provider request fails with an error classified as context-window overflow, the session SHALL compact and retry the request instead of surfacing the error, up to a configured retry cap per run. If compaction cannot shrink the history or retries are exhausted, the original provider error SHALL be surfaced unchanged.

#### Scenario: Overflow recovered

- **WHEN** a provider request fails with a context-overflow error and compaction succeeds in shrinking the history
- **THEN** the request is retried with the compacted history and the run continues without surfacing the overflow error

#### Scenario: Retries exhausted

- **WHEN** the overflow retry cap for a run is exhausted without a successful request
- **THEN** the run ends with the original provider error outcome

### Requirement: Summary replacement shape

Compaction SHALL replace the oldest contiguous span of history with a single summary message that is explicitly marked as a compaction summary (distinguishable from ordinary user or assistant content), SHALL preserve a configured recent tail of messages verbatim, and SHALL NOT split a tool call from its tool result across the compaction boundary.

#### Scenario: Tool pair kept whole

- **WHEN** the compaction boundary would fall between an assistant message containing tool calls and the tool results answering them
- **THEN** the boundary moves so the assistant message and all its tool results end up on the same side

#### Scenario: Summary is marked

- **WHEN** a compacted session's history is inspected (via the API or the log)
- **THEN** the summary message is identifiable as compaction output, not mistakable for words the human user wrote

### Requirement: Compaction must shrink or abort

A compaction whose result would not reduce the token footprint of the history SHALL be rejected: the conversation surface stays untouched and the attempt is observable. A summarization failure (provider error, empty summary) SHALL likewise leave the conversation surface untouched.

#### Scenario: Non-shrinking summary rejected

- **WHEN** the generated summary plus retained tail would be at least as large as the history it replaces
- **THEN** no compaction entry is written and the session's history is unchanged

#### Scenario: Summarization call fails

- **WHEN** the summarization request itself errors
- **THEN** the session's history is unchanged, and (in the reactive path) the original overflow error is surfaced

### Requirement: Compaction is observable

A compaction SHALL be announced on the session's event stream as a typed control-plane event carrying at least the covered span and the resulting summary size, so API consumers can render what happened.

#### Scenario: Event on the stream

- **WHEN** compaction completes during a run streamed over the HTTP API
- **THEN** the SSE stream carries a compaction event before the events of the next provider request

### Requirement: Configuration with safe defaults

Compaction SHALL be configurable per agent (enabled flag, threshold ratio, retained-tail budget, summarization model override, per-run retry cap) with defaults that enable it without any configuration. Invalid compaction configuration SHALL fail fleet compilation with a diagnostic naming the field.

#### Scenario: Zero-config default

- **WHEN** an agent's `agent.yaml` has no compaction block
- **THEN** the agent compiles with compaction enabled under default thresholds

#### Scenario: Invalid config rejected

- **WHEN** an agent sets a compaction threshold ratio outside (0, 1] or a non-numeric retry cap
- **THEN** `knot validate` reports a compile error for that agent naming the offending field
