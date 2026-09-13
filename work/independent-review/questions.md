# Resolved product decision

## Quantity set to zero

Current behavior: editing a ledger-backed holding from a positive quantity to zero appends the
offsetting sale required by the ledger model and removes the position projection, without a
separate confirmation.

Resolved 2026-09-13: zero remains a sell-all shortcut, but only after explicit confirmation.
The confirmed action appends a sale to the ledger and removes the derived holding projection, so
the portfolio stays clean without losing transaction history.

The browser asks before the API call, and the API rejects an unconfirmed zero target for a
ledger-backed holding. Purchases remain positive-only at the API boundary; zero and negative
purchase quantities are refused.
