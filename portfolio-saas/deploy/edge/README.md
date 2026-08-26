# Edge is not in this repository

TLS termination for this VPS lives at `/opt/apps/vps-edge` (compose project
`vps-edge`, Docker network `vps-edge`). Portfolio only joins that network:
`docker-compose.prod.yml` aliases the frontend as `portfolio-frontend`.

Do not bind-mount a Caddyfile from this git tree. A `git reset --hard` would
otherwise rewrite the running proxy.

Bring the application up with `scripts/deploy.sh` after `vps-edge` already
exists. `deploy.sh` in this directory refuses to start Caddy on purpose.
