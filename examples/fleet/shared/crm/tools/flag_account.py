"""Flag a customer account for manual review."""

from __future__ import annotations

from typing import Annotated

from knot.authoring.tools import tool

#: A tiny, deterministic, in-memory set of flagged accounts — no network, no
#: randomness.
_FLAGGED: set[str] = set()


@tool
def flag_account(
    customer_id: Annotated[str, "The customer id to flag for review."],
    reason: Annotated[str, "Why this account is being flagged."],
) -> str:
    """Flag a customer account for manual review by the trust & safety team.

    Gated with a 'once' approval (see shared/crm/bundle.yaml): a human signs
    off the first time an agent uses this tool in a session, and every later
    call in that same session runs ungated.
    """
    _FLAGGED.add(customer_id)
    return f"Flagged {customer_id} for review: {reason}"
