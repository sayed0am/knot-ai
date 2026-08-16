## Purpose

Keeps oversized tool results retrievable by the model on demand — a bounded preview replaces the inline result, while the full content is stored durably and paged back through a retrieval tool — so no context, wire, or log surface carries unbounded payloads.

## ADDED Requirements

### Requirement: Oversized results are replaced by a bounded preview

When a tool result's text exceeds the configured inline byte cap, the model-facing result SHALL be replaced by a head/tail preview of that text plus a notice stating the omitted byte count, a retrieval reference, and how to retrieve the full content. The complete replacement SHALL fit within the inline cap. Results within the cap, and results containing non-text content, SHALL pass through unchanged.

#### Scenario: Large text result spilled

- **WHEN** a tool returns text exceeding the inline cap
- **THEN** the model receives a head/tail preview followed by a notice naming the omitted bytes and the retrieval reference, and the whole replacement is within the cap

#### Scenario: Small result untouched

- **WHEN** a tool returns text within the inline cap
- **THEN** the result reaches the model exactly as produced

### Requirement: Full content is durably retrievable by the model

The full spilled text SHALL be stored durably, keyed to the originating tool call, and a framework-provided retrieval tool SHALL be available to every agent to page through it (offset/limit) and to search it (pattern), returning bounded slices.

#### Scenario: Paging through spilled content

- **WHEN** the model calls the retrieval tool with the reference from a spill notice and an offset/limit
- **THEN** it receives exactly that bounded slice of the full original text

#### Scenario: Searching spilled content

- **WHEN** the model calls the retrieval tool with a search pattern
- **THEN** it receives the matching regions (bounded), each locatable by offset for follow-up paging

#### Scenario: Unknown reference

- **WHEN** the retrieval tool is called with a reference that does not exist in this session
- **THEN** it returns an error result saying so, and the run continues normally

### Requirement: Retrieval is exempt from spilling

The retrieval tool's own results SHALL never be spilled; its output is bounded by its paging parameters instead.

#### Scenario: No spill-retrieve loop

- **WHEN** the retrieval tool returns a slice at its maximum allowed size
- **THEN** that result is delivered inline, not spilled again

### Requirement: Replacement never exceeds the original or the cap

Spilling SHALL never produce a model-facing result larger than the cap or larger than the original. When even a notice-only replacement cannot fit within the cap, the original result SHALL be kept inline unchanged (with the full content still stored when possible).

#### Scenario: Cap too small for the notice

- **WHEN** the inline cap is smaller than the smallest possible notice for a result
- **THEN** the original result stays inline and nothing larger than it is produced

### Requirement: Spill survives restart

Spilled content SHALL be retrievable after a process restart for as long as its session exists, and SHALL be removed with its session.

#### Scenario: Retrieval after restart

- **WHEN** a session parks after a spill, the process restarts, and the resumed model calls the retrieval tool with the earlier reference
- **THEN** the full original text is served exactly as before the restart

### Requirement: Bounded surfaces everywhere

Once a result is spilled, no session surface SHALL carry the full text implicitly: the persisted message entry, streamed events, and provider-visible history all carry the bounded preview and the retrieval reference, not the full content.

#### Scenario: Entry log stays bounded

- **WHEN** a spilled result's message entry is read back from the session log
- **THEN** it contains the preview and reference, not the full original text

### Requirement: Best-effort fallback

A spill-storage failure SHALL NOT fail the tool call: the system falls back to the pre-existing truncation behavior for that result and reports the fallback observably (a warning), never as a tool error.

#### Scenario: Storage write fails

- **WHEN** storing the full content fails for a result over the cap
- **THEN** the model receives the truncated form of the result, the call is not an error, and a warning is emitted
