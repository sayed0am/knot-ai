# http-auth

## Purpose

Provides an opt-in static bearer-token gate for the HTTP API so a knot server can be exposed beyond localhost without a separate auth proxy.

## Requirements

### Requirement: Opt-in bearer-token authentication
`knot serve` SHALL accept a static token via CLI flag or environment variable. When configured, every HTTP endpoint (including SSE streams) SHALL require `Authorization: Bearer <token>` and reject requests without it as unauthorized. When not configured, the API remains open exactly as in v0.

#### Scenario: Token required when configured
- **WHEN** the server is started with a token and a request arrives without a matching bearer header
- **THEN** the request is rejected with 401 and no handler runs

#### Scenario: Valid token accepted
- **WHEN** the server is started with a token and a request carries the matching bearer header
- **THEN** the request proceeds normally, including long-lived SSE streams

#### Scenario: No token configured
- **WHEN** the server is started without a token
- **THEN** all endpoints behave as before, with no auth requirement

### Requirement: Token never disclosed
The configured token MUST NOT appear in logs, error responses, or any API payload.

#### Scenario: Failed auth response is generic
- **WHEN** a request fails authentication
- **THEN** the response reveals neither the expected token nor whether a token is "close"
