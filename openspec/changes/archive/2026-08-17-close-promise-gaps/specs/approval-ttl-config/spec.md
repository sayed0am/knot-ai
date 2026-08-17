## Purpose

Makes the existing approval-expiry path reachable from configuration: authors can set a TTL on approval-gated tools in `agent.yaml` instead of writing a custom decision hook.

## ADDED Requirements

### Requirement: TTL-bearing approval configuration
The `agent.yaml` approvals map SHALL accept, per tool, either the existing bare policy name or an object form that names the policy and an optional `ttl_seconds`. When a TTL is configured, approval requests for that tool carry it, and the existing expiry sweep denies the request once the TTL elapses.

#### Scenario: Bare form still works
- **WHEN** an agent declares `approvals: {send_email: always}` as today
- **THEN** compilation and runtime behavior are unchanged, with no TTL on the request

#### Scenario: Object form sets a TTL
- **WHEN** an agent declares an approval with `policy: always` and `ttl_seconds: 3600` and the tool is called
- **THEN** the pending approval carries the TTL, and if unresolved after an hour it is durably denied with an expiry reason fed back to the model

### Requirement: TTL visible to inbox consumers
Pending approval requests exposed over the HTTP API SHALL include their configured TTL (null when none), so inbox UIs can show time remaining.

#### Scenario: Inbox shows expiry
- **WHEN** a client lists pending approvals
- **THEN** each entry includes its TTL alongside its age
