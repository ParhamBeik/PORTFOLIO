#!/bin/sh
# Run migrations then start the server. Migrate is idempotent and safe on boot.
set -e

echo "Applying migrations..."
python manage.py migrate --noinput

# Seed the asset catalog + a demo user on first boot so the app is usable.
python manage.py seed_assets || true
python manage.py seed_demo || true

echo "Collecting static files..."
python manage.py collectstatic --noinput || true

exec "$@"
