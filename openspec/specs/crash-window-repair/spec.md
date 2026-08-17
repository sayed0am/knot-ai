# crash-window-repair

## Purpose

Ensures that tool executions interrupted by a crash are detected and resolved when serving resumes, so no session is silently stuck in an ambiguous half-executed state.

## Requirements

### Requirement: Serve-time crash-window repair
`knot serve` SHALL detect crash windows (approved-but-unfinished tool executions) across all persisted sessions at startup, before accepting requests, and SHALL automatically re-execute those whose tool is marked idempotent, recording the repair durably in the session's entry log.

#### Scenario: Idempotent tool repaired at startup
- **WHEN** the server starts and a session has a crash window whose tool is declared idempotent
- **THEN** the tool is re-executed and a durable tool result is appended, leaving the session consistent with no operator involvement

#### Scenario: Non-idempotent tool held for the operator
- **WHEN** the server starts and a session has a crash window whose tool is not idempotent
- **THEN** the window is not re-executed and is recorded as needing operator resolution, and the session does not resume until resolved

### Requirement: Operator resolution of unrepairable windows
The HTTP API SHALL expose the set of crash windows needing operator resolution and SHALL let an operator resolve one by skipping it, which durably records an error tool result attributing the skip to the operator.

#### Scenario: Operator lists pending windows
- **WHEN** an operator requests the list of unresolved crash windows
- **THEN** each entry identifies the session, the tool call, and why automatic repair was not possible

#### Scenario: Operator skips a window
- **WHEN** an operator resolves a listed window with a skip
- **THEN** a durable error tool result is appended naming the operator resolution, and the session becomes resumable
