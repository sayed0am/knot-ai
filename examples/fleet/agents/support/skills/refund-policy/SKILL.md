---
name: Refund Policy
description: How to evaluate refund eligibility and process a refund request for a customer order.
---

# Refund Policy

Use this policy whenever a customer asks for a refund, or when you are
deciding whether to offer one proactively.

## Eligibility

1. **Within 30 days of delivery.** Use `lookup_order` to confirm the order
   has actually been delivered, and when. An order that has not shipped yet
   is not eligible for a refund — offer to cancel it instead.
2. **Not a final-sale item.** If the customer or the order notes mention
   "final sale" or "clearance", no refund applies; offer store credit
   instead.
3. **One refund per order.** Never issue more than one refund against the
   same order id.

## Process

1. Confirm the order id and the reason for the refund with the customer.
2. Check eligibility using the two rules above.
3. If eligible, explain the refund amount and timeline (5–7 business days
   back to the original payment method) before taking any action.
4. Any change to the customer's record (crediting an account, adjusting a
   tier, etc.) goes through `update_customer`, which requires a human
   approval — tell the customer you are submitting the change for approval.

## When in doubt

If eligibility is unclear from the order data alone (for example, a dispute
about item condition), delegate to the `researcher` subagent to check for
prior notes on the account before deciding, or ask the customer directly
with `ask_user`.
