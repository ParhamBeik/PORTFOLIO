# Findings

## Fixed — high

**Concurrent last-superuser deactivation.** The uncommitted member-admin endpoint counted
active superusers outside its transaction. Two staff requests could both pass the check and
deactivate the final two roots. The endpoint now locks the active-superuser set in stable order
before deciding, and a real concurrent PostgreSQL integration test proves one request succeeds,
one is rejected, and one active root remains.

## Test coverage repaired

The deep-link routing implementation had no tests for `/signup` or a protected route's return
path. Component-integration tests now cover both behaviors.

## Corrected — confirmation for sell-all

Changing a holding quantity to zero remains an intentional sale and removes the position
projection. It now requires an explicit user confirmation in the browser and a matching API
acknowledgement, so bypassing the dialog cannot create the sale. See `questions.md`.

## Refuted reports

Deleting a reversal restoring its original event is the expected ledger replay result. A
deposit (“Money in”) is correctly credited and passes focused database checks.
