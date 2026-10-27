# Server monitoring and tuning (2026-10-26)

The VPS is 8 GB RAM / 4 cores shared with Bama, so slowdowns come from
contention. Until this change there was no history, so a spike could not be
dated. Now it is recorded every minute.

## Watching the server

```bash
ssh vps /opt/apps/portfolio-repo/portfolio-saas/scripts/ops_report.sh 7   # last 7 days
ssh vps ls /var/log/vps-metrics                                          # raw files
```

`scripts/ops_metrics.sh` (cron, every minute, installed by
`scripts/install_ops_cron.sh`) appends to `/var/log/vps-metrics/`:

| File | Rows | Key columns |
| --- | --- | --- |
| `host-DATE.tsv` | 1/min | load, free RAM, swap used, swap in/out, **iowait %**, disk MB read/written, disk busy %, pressure (psi) |
| `containers-DATE.tsv` | 1/container/min | CPU %, RAM MB, disk MB read/written |
| `pg-DATE.tsv` | 1/5 min | forced vs timed checkpoints, temp-file bytes, sessions, longest query |

Cost: ~2 MB/day, 30 days kept, no extra service. Reading the numbers:

- `iowait` above ~20% for many minutes: the disk is the bottleneck.
- `swapIn` large: RAM ran out and memory was read back from disk.
- `forced_ckpt` high: Postgres flushed early because WAL filled (see `max_wal_size`).
- `temp_spill` high: queries sorted/hashed on disk (see `work_mem`).

Per-query cost needs `pg_stat_statements` (loaded by `docker-compose.prod.yml`).
Once after the first deploy:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production exec db \
  psql -U portfolio -d portfolio -c 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements;'
```

Then, for the queries that read the most from disk:

```sql
select calls, round(total_exec_time/1000) total_s, shared_blks_read, left(query,120)
from pg_stat_statements order by shared_blks_read desc limit 10;
```

Postgres now also logs temp-file spills over 64 MB, queries over 2 s, lock waits
and slow autovacuums (`docker logs portfolio-saas-db-1`).

## What was changed and why

| Change | Where | Evidence (2026-10-26) |
| --- | --- | --- |
| `max_wal_size` 1 GB -> 4 GB, `checkpoint_timeout` 15 min, `wal_compression` | db command | 31-36 forced checkpoints/day on busy days |
| `work_mem` 16 -> 32 MB | db command | 219 GB of temp files in 13.8k spills |
| `jit=off`, `effective_io_concurrency=200` | db command | analytics queries on 4 cores |
| `cpu_shares`: db/API 2048, workers/beat/MinIO 256 | compose | under CPU contention the site wins over backfill |
| MinIO `MINIO_SCANNER_SPEED=slowest`, CPU 0.25 -> 0.5, RAM 256 -> 384 MB | compose | scanner pinned MinIO at its CPU limit, ~1.9 TB read in 3 weeks |
| `swappiness` 10, dirty-page limits, THP `madvise`, core dumps off | `scripts/vps_tune.sh` | swap in use, 19% iowait, THP `always` |

Not changed on purpose: data retention (see `STORAGE-POLICY.md`: keep everything).
