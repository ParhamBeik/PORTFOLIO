# Storage policy (decided 2026-10-02)

Portfolio runs on one 100 GB VPS shared with three other apps, with **no
backups**. Portfolio features compute from its full history, so its data is
**kept in full and compressed losslessly**, never thinned or turned into links.

## Rules

1. **Keep every row and every filing.** Ticks, candles, snapshots, Codal
   reports and their source files are all retained. No retention deletes.
2. **Compress losslessly, transparently.**
   - Ticks: TimescaleDB compresses chunks older than 7 days
     (`marketdata/0020`). Queries and inserts into old days still work.
   - Derivative & market snapshots: `DerivativeSnapshot` and `MarketSnapshot`
     are TimescaleDB hypertables compressed after 7 days (`marketdata/0027`).
   - Codal files: MinIO compresses on write
     (`MINIO_COMPRESSION_*` in `docker-compose.prod.yml`). Readers get identical
     bytes; `checksum_sha256` stays valid.
   - Any new large hypertable gets a compression policy in its migration; any
     new bucket stores through the same MinIO.
3. **External-USB backup policy (approved 2026-11-28).**
   - The VPS keeps strictly **one** rolling `zstd`-compressed encrypted
     PostgreSQL dump in `/var/backups/portfolio/` (no multi-day retention on
     the shared 99 GB SSD).
   - Pull backups and cold archives onto an external USB drive via
     `scripts/pull_vps_archive_to_usb.sh /Volumes/<DRIVE>` (never onto the
     Mac's internal SSD).

## Why

Uncompressed, the 30 newest tick days cost ~8 GB and Codal files ~23 GB. The
disk monitor pauses collectors at 95% use. Lossless compression keeps every
feature working while freeing ~18 GB.

## Cleanup

`manage.py recompress_codal_objects --after-id N --limit M` rewrites objects
stored before MinIO compression was enabled (resumable; verifies checksums).
