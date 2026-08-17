## Purpose

Gives authors a per-session token ceiling so a runaway session stops deterministically, using the provider-reported usage that is already durably persisted per turn.

## ADDED Requirements

### Requirement: Configurable session token budget
An agent SHALL be able to configure a maximum total token budget for a session in its limits configuration. The budget counts provider-reported input and output tokens (including cache reads/writes) accumulated across all runs of the session.

#### Scenario: Budget configured
- **WHEN** an agent sets a session token budget in `agent.yaml`
- **THEN** compilation succeeds and the limit is visible in the compiled manifest

### Requirement: Budget enforcement at turn boundaries
Before each model request, the loop SHALL check accumulated session usage against the budget. When the budget is exhausted, the run SHALL end with an error outcome whose message names the budget and the amount consumed, without making the provider request. Enforcement MUST survive process restarts, since accumulated usage derives from the durable entry log.

#### Scenario: Budget exhausted mid-session
- **WHEN** a session's accumulated tokens meet or exceed the configured budget and the loop is about to start another turn
- **THEN** no provider request is made and the run ends with an error outcome stating that the session token budget is exhausted

#### Scenario: No budget configured
- **WHEN** an agent configures no session token budget
- **THEN** behavior is unchanged and no budget check occurs
