## Purpose

Gives SSE consumers the timing and progress facts they cannot currently derive: when tool executions started and ended, and which loop turn an event belongs to.

## ADDED Requirements

### Requirement: Tool-execution events carry timestamps
Tool-execution start, update, and end events SHALL each carry a millisecond wall-clock timestamp taken when the event is emitted, so a consumer can measure per-tool latency live.

#### Scenario: Latency derivable from one stream
- **WHEN** a consumer receives a tool execution's start and end events over SSE
- **THEN** subtracting their timestamps yields that execution's duration without any out-of-band data

### Requirement: Turn events carry a turn counter
Turn start and end events SHALL carry the 1-based turn number within the current run, so a consumer can render loop progress without counting frames itself.

#### Scenario: Progress rendering
- **WHEN** a run is on its third loop iteration
- **THEN** the emitted turn events carry `turn: 3`

### Requirement: Additive wire compatibility
The new fields SHALL be additive: every previously emitted field keeps its name, casing, and meaning, so existing SSE consumers continue to work unmodified.

#### Scenario: Old consumer unaffected
- **WHEN** a consumer written before this change ignores unknown fields
- **THEN** it processes the event stream exactly as before
