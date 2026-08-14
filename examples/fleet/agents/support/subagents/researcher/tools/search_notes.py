"""Search internal account notes for anything relevant to an order."""

from __future__ import annotations

from typing import Annotated

from knot.authoring.tools import tool

#: A tiny, deterministic, in-memory note store — no network, no randomness.
_NOTES: dict[str, str] = {
    "ORD-1001": (
        "Customer called on 2026-08-10 asking about a delayed shipment; "
        "agent confirmed carrier tracking was updated."
    ),
    "ORD-1003": (
        "Customer previously flagged this order as a gift and asked for gift receipts only."
    ),
}


@tool
def search_notes(
    order_id: Annotated[str, "The order id to search internal account notes for."],
) -> str:
    """Search internal account notes for anything relevant to an order.

    Gated behind a human approval (see this subagent's own agent.yaml) since
    account notes may contain sensitive history.
    """
    note = _NOTES.get(order_id)
    if note is None:
        return f"No notes found for order {order_id!r}."
    return note
