# Independent claim ledger

Target task recovered: `01a0951d-e334-7683-b206-05d69604d659` — **Audit and fix app bugs**.
The later `01a096f7-29dc-7390-9775-d88440e732cf` audit is supporting evidence, not
accepted on trust. Its uncommitted work was already present when this review began.

| Previous claim | Independent evidence and method | Verdict | Follow-up |
| --- | --- | --- | --- |
| A synthetic account exposed stale portfolio notes after a holding change, and `26ee30a` fixes it. | Inspected the committed revision: account reload increments a shared revision and the notes request depends on it. Remote `main`, CI run `34702621654`, and deployed revision all resolve to `26ee30a`. | VERIFIED (implementation and release); browser reproduction is not repeated because it would mutate the owner's live holdings. | None for the deployed revision. |
| The release was pushed, CI-passed, deployed, and live. | `git ls-remote`, GitHub run metadata, and read-only live health/readiness/prices checks all independently matched `26ee30a`. | VERIFIED | None. |
| Registration can be opened or closed through configuration and its public status is safe. | Reviewed the settings, public endpoint, route allow-list, focused tests, full PostgreSQL suite, and live public status endpoint. | VERIFIED | `c87eee3` is deployed; the live status accurately reports self-service reset unavailable without a relay. |
| Signed-out public legal and reset pages, titles, risk-loading behavior, comparison wording, and Lighthouse fixture changes are correct. | Reviewed every attributed commit diff; the terminal CI frontend job passed lint, unit/Vitest, build, Lighthouse, and headers for `26ee30a`. | VERIFIED for the deployed commit. | Browser re-execution of anonymous pages is limited by the shared authenticated session; CI is the independent executable evidence. |
| The later audit's `F-005` (“deleting a reversal resurrects a holding”) is corruption. | Ledger deletion removes the reversal; replay then correctly makes the original event active again. Existing reversal/projection tests cover that model. | REFUTED | Consider clearer UI wording only if desired. |
| The later audit's `F-006` (“Money in” is rejected for insufficient cash) is a product defect. | The UI maps Money in to `deposit`; the ledger credits deposits. Two focused PostgreSQL tests passed, including a deposit after prior trades. | REFUTED | The reported live observation was not reproduced. |
| The later audit's zero-quantity finding is a confirmed data-integrity defect. | Source shows zero is intentionally modelled as an offsetting sale and ledger replay removes the position. The surprise was UX, not an integrity failure; confirmation is now required by both browser and API. | VERIFIED AFTER CORRECTION | Released as `c87eee3`; live mutation intentionally not performed against owner data. |
