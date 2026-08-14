"""Shared low-level types for knot's provider layer."""

from __future__ import annotations

# Pydantic needs PEP 695 named recursive aliases for JSON-like values.
type JSONPrimitive = str | int | float | bool | None
type JSONValue = JSONPrimitive | list[JSONValue] | dict[str, JSONValue]
type JSONObject = dict[str, JSONValue]

__all__ = ["JSONObject", "JSONPrimitive", "JSONValue"]
