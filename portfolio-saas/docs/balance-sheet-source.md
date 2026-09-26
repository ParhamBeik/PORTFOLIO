# Balance-sheet source and backfill boundary

The first supported statements are two public فولاد V9 sheets: `sheetId=0` standalone at 1405/03/31 and `sheetId=14` consolidated at 1404/12/29. Their source HTML fixtures are `tests/fixtures/codal/foolad_interim_balance_v9.html` (SHA-256 `43bd865c2f37054e766fe1e7abecf4edbf141e8be9b6fe7307d45650ea68f9cc`) and `foolad_annual_consolidated_balance_v9.html` (SHA-256 `bcf3d8e6269286cff66fe463616c28509f156ae148d40e54a25c81525210a329`). Each sheet is a separate public Codal response. The income HTML already archived for the filing advertises the balance sheet in its sheet selector; the URL is derived only from that selector and an allowlisted Codal filing URL.

After a controlled deployment, inspect a bounded range before writing:

```bash
python manage.py backfill_balance_sheets --symbol فولاد --limit 20 --fetch --dry-run
python manage.py backfill_balance_sheets --symbol فولاد --limit 20 --fetch
```

`--fetch` explicitly permits a Codal HTTP request for a missing balance sheet; omitting it uses archived sheets only. `--dry-run` performs validation without database or object-store writes. `--after-id` resumes by original income artifact ID, and the command limits each batch to at most 100 source artifacts. On a real run it stores the new response by content hash before candidate facts are appended. Rerunning the same bytes and parser version skips the existing extraction.

The parser accepts only the observed table codes and row labels, the filing's exact issuer, reporting date, audited/consolidated flags, million-Rial denomination, and current-period cells. It requires current plus noncurrent assets to equal total assets, current plus noncurrent liabilities to equal total liabilities, and assets to equal liabilities plus equity. Balance sheets are point-in-time facts: `period_start` is empty. These checks validate transcription and internal arithmetic, not the issuer's accounting or a USD conversion. An unprocessed newer correction withholds the older reading from Explore and research. Unsupported templates and generic “debt” questions abstain.

Production has not run this command or released these balance points. The 100 most recent parent-like stored statement HTML artifacts sampled on 2026-09-26 contained no supported V9 template; coverage needs a measured backfill before cross-company screening.
