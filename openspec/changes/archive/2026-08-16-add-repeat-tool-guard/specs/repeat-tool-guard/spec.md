## Purpose

Breaks unattended tool-call repetition loops early by injecting escalating advisory reminders when the model keeps issuing the identical call, without ever blocking, delaying, or rewriting a call.

## ADDED Requirements

### Requirement: Consecutive identical calls are detected

The system SHALL track, per live run, consecutive tool calls sharing the same key of (tool name, canonicalized arguments), where canonicalization is insensitive to object property order. A tracked call with a different key SHALL reset the count to 1.

#### Scenario: Property order does not defeat detection

- **WHEN** the model calls the same tool twice with argument objects that differ only in property order
- **THEN** both calls count toward the same consecutive run

#### Scenario: Different arguments reset the chain

- **WHEN** the model calls the same tool with materially different arguments
- **THEN** the consecutive count restarts at 1

### Requirement: Escalating advisories at thresholds

When a consecutive run reaches a configured threshold, the system SHALL inject an advisory message into the conversation before the next provider request. The first threshold's advisory SHALL be a short generic nudge; later thresholds' advisories SHALL name the tool, the run length, and a preview of the repeated arguments bounded by a configured character cap. Detection SHALL always compare full canonical arguments; the cap bounds only the preview.

#### Scenario: First threshold

- **WHEN** a run of identical calls reaches the first configured threshold
- **THEN** the model's next request contains a short generic advisory to re-read the last result and change approach or conclude

#### Scenario: Later threshold names specifics

- **WHEN** the run reaches a later configured threshold
- **THEN** the advisory names the tool, the consecutive count, and a bounded preview of the arguments

### Requirement: Advisory-only, never restrictive

The guard SHALL NOT deny, delay, rewrite, or reorder any tool call, SHALL NOT appear in the tool list, and SHALL NOT alter any tool result. Every repeated call still executes (or is denied) exactly as it would without the guard.

#### Scenario: Threshold call still executes

- **WHEN** a call crosses an advisory threshold
- **THEN** that call executes normally and its result is identical to what it would be without the guard

### Requirement: Excluded tools are transparent to the chain

Tools matching the configured exclusion list SHALL neither increment nor reset a consecutive run: identical tracked calls separated only by excluded calls still count as consecutive.

#### Scenario: Bookkeeping interleave does not launder a loop

- **WHEN** the model alternates an identical tracked call with an excluded tool call
- **THEN** the tracked call's consecutive count keeps growing across the interleaving

### Requirement: Denied calls count

Calls resolved by denial (decision hook or unknown tool) SHALL count toward the chain like executed calls.

#### Scenario: Hammering a denied call

- **WHEN** the model repeats an identical call that the decision hook denies every time
- **THEN** the consecutive count grows and advisories fire at the configured thresholds

### Requirement: New human input resets the chain

A new user or steering message entering the conversation SHALL reset the consecutive count.

#### Scenario: Steering resets

- **WHEN** a steering message is delivered between turns during a run
- **THEN** any in-progress consecutive run restarts from zero afterward

### Requirement: Advisories are durable model-visible input

An injected advisory SHALL flow through the session's durable entry log like any model-visible message, distinguishable from words the human user wrote, and SHALL satisfy the model-visible-means-logged invariant.

#### Scenario: Replay includes the advisory

- **WHEN** a session that received an advisory is rehydrated after restart
- **THEN** the derived history contains the advisory at the same position the live run saw it

### Requirement: Chain state is per-run only

The consecutive count SHALL NOT persist across park/resume or restart: a resumed session starts with a fresh chain. (Advisories already delivered remain in history; only the counter is transient.)

#### Scenario: Fresh chain after resume

- **WHEN** a session parks mid-run and later resumes
- **THEN** the first tracked call after resume starts a new consecutive run at 1

### Requirement: Configuration validated at compile time

Thresholds (default `[3, 5, 8]`), the exclusion list, and the argument-preview cap SHALL be configurable per agent. Invalid configuration — an empty threshold list, a non-integer, a threshold below 2, duplicates, or a non-positive preview cap — SHALL fail fleet compilation with a diagnostic naming the field. The guard SHALL be enabled by default with the default thresholds.

#### Scenario: Invalid thresholds rejected

- **WHEN** an agent configures `thresholds: [3, 3]` or `thresholds: [1]`
- **THEN** `knot validate` reports a compile error for that agent naming the offending value

#### Scenario: Zero-config default

- **WHEN** an agent's `agent.yaml` has no repeat-guard block
- **THEN** the agent compiles with the guard enabled at thresholds `[3, 5, 8]`
