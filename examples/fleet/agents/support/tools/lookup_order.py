"""Look up a customer order's shipping status."""

from __future__ import annotations

from typing import Annotated

from knot.authoring.tools import tool

#: A tiny, deterministic, in-memory order book — no network, no randomness,
#: so this tool is safe to call in tests and demos alike.
_ORDERS: dict[str, dict[str, str | None]] = {
    "ORD-1001": {"status": "shipped", "carrier": "UPS", "eta": "2026-08-16"},
    "ORD-1002": {"status": "processing", "carrier": None, "eta": None},
    "ORD-1003": {"status": "delivered", "carrier": "USPS", "eta": "2026-08-05"},
}


@tool(idempotent=True)
def lookup_order(
    order_id: Annotated[str, "The order id to look up, e.g. 'ORD-1001'."],
) -> str:
    """Look up an order's current shipping status by its order id.

    Returns a short, human-readable summary. Read-only, so this is safe to
    call as often as needed.
    """
    order = _ORDERS.get(order_id)
    if order is None:
        return f"No order found with id {order_id!r}."
    if order["status"] == "shipped":
        return f"Order {order_id} has shipped via {order['carrier']}, ETA {order['eta']}."
    if order["status"] == "delivered":
        return f"Order {order_id} was delivered via {order['carrier']} on {order['eta']}."
    return f"Order {order_id} is still processing and has not shipped yet."
