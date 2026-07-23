#!/bin/sh
# Run migrations then start the server. Migrate is idempotent and safe on boot.
set -e

echo "Applying migrations..."
python manage.py migrate --noinput

# Seed the asset catalog so valuations have something to look up. Always run —
# it's idempotent and the catalog is needed in every environment.
python manage.py seed_assets

# Demo user is dev-only: never auto-create accounts in production.
if [ "${DJANGO_DEBUG:-1}" = "1" ]; then
  python manage.py seed_demo || true
  # Load the real Father/Mother portfolios (exact holdings + real daily history)
  # from the tracker data files. Idempotent: re-runs correct the state.
  python manage.py import_real_portfolios --data-dir "${REAL_PORTFOLIO_DATA_DIR:-/portfolio-data}" || true
fi

echo "Collecting static files..."
python manage.py collectstatic --noinput || true

exec "$@"
