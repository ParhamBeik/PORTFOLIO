#!/bin/sh
# Run migrations then start the server. Migrate is idempotent and safe on boot.
set -e

echo "Applying migrations..."
python manage.py migrate --noinput

# Seed the asset catalog so valuations have something to look up. Always run —
# it's idempotent and the catalog is needed in every environment.
python manage.py seed_assets || true

# Demo user is dev-only: never auto-create accounts in production.
if [ "${DJANGO_DEBUG:-1}" = "1" ]; then
  python manage.py seed_demo || true
fi

echo "Collecting static files..."
python manage.py collectstatic --noinput || true

exec "$@"
