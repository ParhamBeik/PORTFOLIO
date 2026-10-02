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
   - Codal files: MinIO compresses on write
     (`MINIO_COMPRESSION_*` in `docker-compose.prod.yml`). Readers get identical
     bytes; `checksum_sha256` stays valid.
   - Any new large hypertable gets a compression policy in its migration; any
     new bucket stores through the same MinIO.
3. **No backups or second copies** on the VPS, the Mac or external drives,
   until a backup plan is written down and approved.

## Why

Uncompressed, the 30 newest tick days cost ~8 GB and Codal files ~23 GB. The
disk monitor pauses collectors at 95% use. Lossless compression keeps every
feature working while freeing ~18 GB.

## Cleanup

`manage.py recompress_codal_objects --after-id N --limit M` rewrites objects
stored before MinIO compression was enabled (resumable; verifies checksums).
