# PORTFOLIO

@AGENTS.md

The app lives in `portfolio-saas/`. Its rules load from there:

@portfolio-saas/CLAUDE.md

Structure and layering: `portfolio-saas/ARCHITECTURE.md`. Which docs are live vs history:
`portfolio-saas/docs/README.md`.

## VPS (production)

- `ssh vps` (alias in `~/.ssh/config`; root, key `~/.ssh/vps_45_139_10_12`, connection reused 10 min).
- App checkout: `/opt/apps/portfolio-repo/portfolio-saas`; secrets: `/opt/apps/portfolio-secrets`; edge proxy: `/opt/apps/vps-edge` (not ours — don't touch).
- Shared box: `bama`, `newsintel`, `twitter-saas` stacks run beside `portfolio-saas-*`. Only act on `portfolio-saas-*` containers. 99 GB disk, no backups — see `portfolio-saas/docs/STORAGE-POLICY.md`.
- Inspect freely (`docker ps`, `docker logs`, `compose exec ... manage.py shell`). Ask before anything that restarts, deletes, migrates or deploys. Deploy = `scripts/deploy.sh` (see `portfolio-saas/README.md`).
- Batch remote commands into one `ssh vps '...'` call.
- **Cloud sessions:** SSH from the cloud sandbox is blocked on the way in (TCP connects, the SSH banner is dropped; HTTPS gets through). Run `ssh vps` on Parham's Mac through the device / Remote Control tools, not in the sandbox. If the Mac is unreachable, say so — don't try to work around it.
