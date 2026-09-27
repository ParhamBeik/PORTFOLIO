# Historical Codal announcement discovery

The normal `codal_announcements` archive state verifies only the newest five pages per symbol. `backfill_codal_history` creates separate, inclusive Jalali publication-date windows for **completed years** and scans each page through the existing quota-metered fetcher. A window becomes `verified_complete` only when the provider's reported count equals the distinct issuer-aware keys across all pages and every key reads back from PostgreSQL. Invalid dates, out-of-range rows, changing counts, duplicates, and rejected rows leave it incomplete. Large windows split in half until each needs at most `--max-pages`; the split parent itself is never called complete.

```bash
python manage.py backfill_codal_history --start-year 1395 --end-year 1404
python manage.py backfill_codal_history --start-year 1402 --end-year 1404 --symbols فولاد --max-requests 50 --execute
python manage.py backfill_codal_history --start-year 1402 --end-year 1404 --symbols فولاد --recheck-after-days 365 --execute
```

The first command is read-only. `--execute` creates durable windows and spends at most `--max-requests` provider calls in that run, counting even a quota-refused attempt conservatively. Defaults are 100 requests and 10 pages per window. A run reserves enough of its local budget for the largest allowed window before starting one, so it can finish or split a window without crossing the specified cap. Provider daily and five-minute admission still comes from the shared quota service. Re-run the same command to continue; completed windows are skipped. Failed windows back off for one to 24 hours. A provider quota refusal ends the run and leaves the window pending.

The read-only production census on 2026-09-27 found 1,593 eligible TSETMC stocks, with stored announcements concentrated in 1403–1405; only 24 symbols had a stored publication dated five years back. Ten annual windows across that catalog require at least 15,930 requests before pagination or splits, so this is intentionally a quota-paced multi-run acquisition. The source's date-filter behavior must be validated with a narrow executed run before a universe-wide rollout. No production backfill has run, and this command is not on an automatic schedule.

Completeness is scoped to the **source response for that symbol and date range at `last_success_at`**. It does not prove the issuer's full historical lineage, source immutability, archived report bytes, parsed financial facts, or research-grade coverage. Those are separate checks. Use `--recheck-after-days` to reopen old verifications for corrections; a fresh recheck does not reopen the same window on the next run.
