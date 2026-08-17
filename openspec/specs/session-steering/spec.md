# session-steering

## Purpose

Lets a live human inject mid-run guidance into a running session over HTTP, closing the gap where steering exists in the core harness but has no external surface.

## Requirements

### Requirement: Steer a running session over HTTP
The HTTP API SHALL accept a steering message for a session whose run is currently in progress. The message SHALL be delivered into the running loop at the next turn boundary and SHALL be durably logged as model-visible input, consistent with the model-visible-logged invariant.

#### Scenario: Guidance reaches a live run
- **WHEN** a session is mid-run and a client posts a steering message
- **THEN** the request is accepted, and the loop incorporates the message at its next turn without restarting the run

#### Scenario: Steering is on the record
- **WHEN** a steering message has been delivered
- **THEN** it appears in the session's durable entry log and in the transcript exposed by the API

### Requirement: Steering rejected when nothing is running
A steering request for a session with no run in progress SHALL be rejected with a conflict error that directs the caller to the ordinary message endpoint, so the two input channels stay distinct.

#### Scenario: Idle session
- **WHEN** a client posts a steering message to an idle or parked session
- **THEN** the API responds with a conflict error and no entry is written
