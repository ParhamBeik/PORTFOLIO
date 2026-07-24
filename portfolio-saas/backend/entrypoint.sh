#!/bin/sh
# Run migrations then start the server. Migrate is idempotent and safe on boot.
set -e

echo "Applying migrations..."
python manage.py migrate --noinput

# Seed the asset catalog so valuations have something to look up. Always run —
# it's idempotent and the catalog is needed in every environment.
python manage.py seed_assets

# Demo user is dev-only: never auto-create accounts in production. Fail closed —
# an unset/ambiguous DJANGO_DEBUG must NOT seed demo data (mirrors the settings.py fix).
if [ "${DJANGO_DEBUG:-0}" = "1" ]; then
  python manage.py seed_demo || echo "Notice: seed_demo command completed or already seeded."
  if [ -d "${REAL_PORTFOLIO_DATA_DIR:-/portfolio-data}" ]; then
    python manage.py import_real_portfolios --data-dir "${REAL_PORTFOLIO_DATA_DIR:-/portfolio-data}"
  else
    echo "Notice: Real portfolio data directory '${REAL_PORTFOLIO_DATA_DIR:-/portfolio-data}' not found, skipping real portfolio import."
  fi
fi


echo "Collecting static files..."
python manage.py collectstatic --noinput

exec "$@"
