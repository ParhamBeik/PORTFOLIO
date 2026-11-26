# PORTFOLIO

@AGENTS.md

The app lives in `portfolio-saas/`. Its rules load from there:

@portfolio-saas/CLAUDE.md

Structure and layering: `portfolio-saas/ARCHITECTURE.md`. Living docs are listed in
`portfolio-saas/README.md` → Docs; everything else is in git history.

## VPS (production)

- Connection and batching rules: see the global VPS section. Portfolio-specific facts follow.
- App checkout: `/opt/apps/portfolio-repo/portfolio-saas`; secrets: `/opt/apps/portfolio-secrets`; edge proxy: `/opt/apps/vps-edge` (not ours — don't touch).
- Shared box: the `bama` stack runs beside `portfolio-saas-*`. Only act on `portfolio-saas-*` containers. 99 GB disk, no backups — see `portfolio-saas/docs/STORAGE-POLICY.md`.
- Inspect freely (`docker ps`, `docker logs`, `compose exec ... manage.py shell`). Ask before anything that restarts, deletes, migrates or deploys. Deploy = `scripts/deploy.sh` (see `portfolio-saas/README.md`).
- **Cloud sessions:** SSH from the cloud sandbox is blocked on the way in (TCP connects, the SSH banner is dropped; HTTPS gets through). Run `ssh vps` on Parham's Mac through the device / Remote Control tools, not in the sandbox. If the Mac is unreachable, say so — don't try to work around it.
