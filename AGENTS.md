# Agent notes

Before changing anything that stores data (tables, hypertables, buckets, volumes, caches), read and follow [portfolio-saas/docs/STORAGE-POLICY.md](portfolio-saas/docs/STORAGE-POLICY.md). The VPS has no backups and a 100 GB disk shared by four apps. Portfolio history is kept in full and compressed losslessly; never thin it or replace it with links.
