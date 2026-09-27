# Research evidence coverage

The daily 04:30 Tehran scan measures what Explore could display for the current catalog of eligible TSETMC stocks. It examines filing periods ending in the preceding 365 days. ETFs, ineligible symbols, and other asset classes are outside this denominator. This is a current-catalog census, not a historical peer universe.

For every stock, the scan calls the same monthly-sales, income, and balance-sheet eligibility readers as Explore. A newer unverified correction withholds the earlier figure. Each family records the number of observed latest filing periods, verified periods, and withheld periods. A symbol can have both verified and withheld periods. “Fully verified” means every observed period **within this window** passed; a stock with no filing is never counted as fully verified.

A completed scan is stored with its start and finish times, window, parser versions, aggregate counts, and per-symbol decisions. Ops shows the latest aggregate and marks it stale after 36 hours. Staff can inspect every symbol in the read-only `ResearchCoverageSnapshot` Django admin record. If a scan fails, the last complete snapshot remains and the workflow ledger records the failure. Snapshots older than 90 days are pruned after a successful scan.

The scan reads existing database evidence and makes no AI or provider request. It checks the same stored-artifact metadata, checksums, coordinates, and revision rules used by Explore; it does not reread every MinIO object. A verified period is not proof of complete historical coverage, peer comparability, issuer accounting accuracy, or a USD conversion. Keep cross-company rankings and USD-growth answers withheld until their separate universe, identity, source, and conversion gates pass.
