# usage-cost-reporting

## Purpose

Makes the USD cost field on the wire usage model honest: present only when the provider actually reported a cost, absent otherwise, so consumers can distinguish "unknown" from "zero".

## Requirements

### Requirement: Cost is nullable and never fabricated
The usage model's `cost` field SHALL be null unless the model provider reported an actual cost for the response. The system MUST NOT emit an all-zeros cost object as a stand-in for unknown cost.

#### Scenario: Provider reports cost
- **WHEN** a response comes from a provider that reports per-response USD cost (e.g., via litellm)
- **THEN** `usage.cost` carries the reported values and is persisted with the assistant message

#### Scenario: Provider does not report cost
- **WHEN** a response comes from a provider that does not report cost
- **THEN** `usage.cost` is null on the wire and in the persisted entry

### Requirement: Cost passes through unmodified
When populated, cost values SHALL be the provider-reported figures passed through without recomputation, so the wire value is attributable to the provider rather than to a knot-maintained price table.

#### Scenario: No local price table
- **WHEN** a provider reports a total cost without a per-category breakdown
- **THEN** the total is emitted as reported and absent categories are null or omitted, not computed locally
