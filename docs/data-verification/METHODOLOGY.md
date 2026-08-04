# Cross-source data verification — methodology

Branch: `phase/cross-source-data-verification`
Audit date: 2026-08-04 Gregorian / 1405-05-13 Jalali
Scope: stored financial data vs. its original provider and, where reachable, an
independent secondary source.

This audit **reads**. It changes no production record and no calculation logic.

## Why the previous artifacts were discarded

The untracked `docs/data-verification/` on this branch previously held
`audit_script.py`, which set:

```python
original_val = stored_val   # "Assume database unadjusted close matches original provider Rial close"
```

and then reported `MATCH`. That compares the database to itself: it cannot fail,
and it produced a clean bill of health for data that this audit shows is in
places wrong. It was deleted rather than extended, along with the
`assets_mapping.json` and `comparisons.json` it generated.

## Data sources of record

| Layer | What it is | How obtained |
|---|---|---|
| Stored | Authoritative Docker Postgres (`portfolio-saas-db-1`) | `audit_db.py`, SELECT only |
| Original provider | BrsApi.ir, the single upstream for every asset class | `audit_provider.py`, 16 live requests |
| Secondary | tgju.org, shakhesban.com, CoinGecko | `secondary_sources.json`, public read-only GETs |

## Safety constraints observed

* **Database identity asserted before querying.** `audit_db.py` exits unless
  `DATABASES['default']['HOST']` is the Docker service `db`, then records
  `current_database()`, `inet_server_addr()` and database size in its output.
  Verified: `portfolio` / `172.18.0.2` / 5694 MB / PostgreSQL 16.4. The host
  machine's own Postgres on :5432 was never contacted.
* **Read-only.** No `.save()`, `.create()`, `.update()`, `.delete()`, no
  migrations, no management command that writes, no Celery task dispatched.
* **No unrestricted ingestion.** `audit_provider.py` calls fetchers directly and
  never calls any `ingest_*` function, so nothing fetched can reach the database.
* **Bounded provider cost.** 16 requests against a 9,800/day quota that stood at
  8 used. Every endpoint used is `HISTORICAL_FULL` — one request returns the
  whole series — so no date-walking was performed.
* **No secrets stored.** The API key is redacted to `***` in
  `provider_hashes.json`. Only the SHA-256 and byte length of each response body
  are recorded, never the body itself.

## Sample construction

Nine instruments across five asset classes, all confirmed present in the
authoritative database before selection. Instruments were chosen to span
*conditions*, not just names: a frequently-traded equity, a less-liquid one, an
instrument carrying a corporate-action adjustment, instruments requiring
currency conversion, and one with a known integrity concern. Per-instrument
rationale is in `db_facts.json → sample_rationale`.

Dates per instrument: the most recent **complete** stored day, one ordinary
historical day, one older historical day, plus condition-specific days
(adjustment boundary, the suspect row). Jalali 1405-05-13 — the current day — is
excluded everywhere: an in-progress session is not a settled daily close.

## Comparison rules

For each (instrument, date), `build_comparisons.py` computes
`ratio = stored / provider` and compares it against the ratio the pipeline is
*supposed* to produce (`expect_ratio`: 1.0 for no conversion, 0.1 for a declared
Rial→Toman division).

| Verdict | Meaning |
|---|---|
| `MATCH` | Stored equals provider within tolerance, no conversion expected |
| `EXPLAINED DIFFERENCE` | Difference is fully accounted for by a declared unit conversion, or by same-day drift on the newest stored day |
| `UNEXPLAINED DIFFERENCE` | A real gap no unit, calendar, or adjustment rule accounts for |
| `NOT VERIFIED` | One or both sides had no value; **never** recorded as a match |

**Tolerances.** Prices are stored as exact `DecimalField` and no rounding is
applied on either side, so for a settled historical day the tolerance is
`0.0001%` — present only to absorb float noise in the ratio, not to excuse
disagreement. The newest stored day (`1405-05-12`) carries a separate `1%`
same-day drift band, because the archive holds the value captured at ingest time
while a re-fetch returns the quote as of now; that band is deliberately **not**
extended to older days, where the same gap would be a genuine discrepancy.

A stored value that is exactly 1/10 of a provider value the provider itself
labels `تومان` is classified `UNEXPLAINED DIFFERENCE`, not explained away as
"probably a unit thing".

## Reproducing

```bash
cd docs/data-verification

# 1. Stored side (read-only, inside the backend container)
docker exec -i portfolio-saas-backend-1 python - < audit_db.py > db_facts.json

# 2. Provider side (~16 live requests; check quota headroom first)
docker exec -i portfolio-saas-backend-1 python - < audit_provider.py > provider_facts.json

# 3. Join into the comparison table (pure local transform)
python3 build_comparisons.py
```

Step 3 is deterministic and re-runnable. Steps 1 and 2 re-read live state, so
provider values and the newest stored day will move; the response hashes in
`provider_hashes.json` pin exactly which payloads backed the findings in
`REPORT.md`.

## Known limits of this method

* A nine-instrument sample cannot establish that a 3.4-million-row warehouse is
  correct. It establishes that specific rules hold or fail on specific rows.
* `tsetmc.com` was unreachable from this network, so the TSE Rial/Toman verdict
  rests on secondary field-name evidence and provider self-consistency rather
  than on the exchange itself. See `secondary_sources.json → unreachable_sources`.
* Provider agreement is not truth. Where the provider and the warehouse agree but
  no secondary source was reachable, the finding says so.
