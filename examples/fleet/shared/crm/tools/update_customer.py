"""Update one field on a stored customer record."""

from __future__ import annotations

from typing import Annotated

from knot.authoring.tools import tool

#: A tiny, deterministic, in-memory customer store — no network, no
#: randomness, so this tool is safe to call in tests and demos alike.
_CUSTOMERS: dict[str, dict[str, str]] = {}


@tool
def update_customer(
    customer_id: Annotated[str, "The customer id to update, e.g. 'CUST-42'."],
    field: Annotated[str, "The customer field to change, e.g. 'email' or 'tier'."],
    value: Annotated[str, "The new value for that field."],
) -> str:
    """Update one field on a customer record.

    This mutates shared customer data, so it is gated behind an 'always'
    approval (see shared/crm/bundle.yaml) — every call requires human
    sign-off before it runs.
    """
    _CUSTOMERS.setdefault(customer_id, {})[field] = value
    return f"Updated {customer_id}.{field} = {value!r}."
