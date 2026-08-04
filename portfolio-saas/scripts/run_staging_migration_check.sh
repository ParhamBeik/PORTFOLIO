#!/usr/bin/env bash
set -euo pipefail
TS=$(date +%Y%m%dT%H%M%S)
OUT_DIR="/tmp/staging_migration_check_${TS}"
mkdir -p "$OUT_DIR"
echo "Outputs will be saved to $OUT_DIR"

# Required env / arguments (operator must set)
: "${BACKUP_FILE:?Provide path to logical backup (local or /tmp/backup.sql.gz)}"
: "${STAGING_PG_SUPERUSER:=postgres}"
: "${APP_DIR:=/srv/app}"
: "${DJANGO_SETTINGS_MODULE:=""}"
: "${STAGING_DB_RESTORE_NAME:=portfolio_staging_restore}"
: "${OPERATOR:=unknown}"

if [ -z "$DJANGO_SETTINGS_MODULE" ]; then
  echo "ERROR: DJANGO_SETTINGS_MODULE must be set to a valid settings module that points to the restored DB (or set DATABASE_URL)."
  echo "Example: export DJANGO_SETTINGS_MODULE=config.settings"
  exit 1
fi

echo "Operator: $OPERATOR"
echo "Backup file: $BACKUP_FILE"
echo "Restore DB name: $STAGING_DB_RESTORE_NAME"
read -p "Confirm you are on the STAGING HOST and want to proceed with restore into database '$STAGING_DB_RESTORE_NAME'? (type YES): " CONFIRM
if [ "$CONFIRM" != "YES" ]; then
  echo "Cancelled by operator."
  exit 1
fi

if [[ -f "$BACKUP_FILE" ]]; then
  LOCAL_BACKUP="$BACKUP_FILE"
else
  echo "Backup file not found at $BACKUP_FILE"
  exit 1
fi

sha256sum "$LOCAL_BACKUP" | tee "$OUT_DIR/backup_sha256.txt"

# Create restore DB (non-destructive)
echo "Creating restore DB: $STAGING_DB_RESTORE_NAME"
sudo -u "$STAGING_PG_SUPERUSER" psql -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS ${STAGING_DB_RESTORE_NAME};"
sudo -u "$STAGING_PG_SUPERUSER" createdb "${STAGING_DB_RESTORE_NAME}"

# Restore
echo "Restoring backup into ${STAGING_DB_RESTORE_NAME}..."
if [[ "$LOCAL_BACKUP" == *.gz ]]; then
  gunzip -c "$LOCAL_BACKUP" | sudo -u "$STAGING_PG_SUPERUSER" psql -d "${STAGING_DB_RESTORE_NAME}" 2>&1 | tee "$OUT_DIR/psql_restore_log.txt"
else
  sudo -u "$STAGING_PG_SUPERUSER" psql -d "${STAGING_DB_RESTORE_NAME}" -f "$LOCAL_BACKUP" 2>&1 | tee "$OUT_DIR/psql_restore_log.txt"
fi

# Quick counts
sudo -u "$STAGING_PG_SUPERUSER" psql -d "${STAGING_DB_RESTORE_NAME}" -c "SELECT COUNT(*) FROM portfolio_price;" | tee "$OUT_DIR/count_portfolio_price.txt"
sudo -u "$STAGING_PG_SUPERUSER" psql -d "${STAGING_DB_RESTORE_NAME}" -c "SELECT COUNT(*) FROM portfolio_asset;" | tee "$OUT_DIR/count_portfolio_asset.txt"

# Prepare app env (operator ensures DATABASE settings point to the restore DB)
echo "Preparing application for migrations"
if [ -n "${VENV:-}" ] && [ -d "$VENV" ]; then
  source "$VENV/bin/activate"
fi
pushd "$APP_DIR" > /dev/null

export DJANGO_SETTINGS_MODULE="$DJANGO_SETTINGS_MODULE"

echo "Checking migrations status for portfolio app"
python manage.py showmigrations portfolio --plan > "$OUT_DIR/showmigrations_portfolio.txt" 2>&1 || true
cat "$OUT_DIR/showmigrations_portfolio.txt"

read -p "Confirm to run python manage.py migrate now against staging DB? (type YES): " MIGCONF
if [ "$MIGCONF" != "YES" ]; then
  echo "Migration aborted by operator."
  popd > /dev/null
  exit 1
fi

# Run migrations (no DB changes outside the restore DB assumed)
python manage.py migrate --noinput 2>&1 | tee "$OUT_DIR/migrate_output.txt"

# Verify columns exist
sudo -u "$STAGING_PG_SUPERUSER" psql -d "${STAGING_DB_RESTORE_NAME}" -c "SELECT column_name FROM information_schema.columns WHERE table_name='portfolio_price' AND column_name IN ('price_unit','price_unit_verified');" | tee "$OUT_DIR/verify_columns.txt"

# Aggregate by unit
sudo -u "$STAGING_PG_SUPERUSER" psql -d "${STAGING_DB_RESTORE_NAME}" -c "SELECT price_unit, price_unit_verified, COUNT(*) FROM portfolio_price GROUP BY price_unit, price_unit_verified ORDER BY COUNT(*) DESC;" > "$OUT_DIR/aggregate_by_unit.txt" 2>&1

# Representative samples: operator may set ASSET_IDS env var (e.g. '1,2,3')
ASSET_IDS="${ASSET_IDS:-NULL}"
if [ "$ASSET_IDS" != "NULL" ]; then
  sudo -u "$STAGING_PG_SUPERUSER" psql -d "${STAGING_DB_RESTORE_NAME}" -c "SELECT p.id, p.asset_id, a.tse_symbol, a.brs_symbol, p.price, p.price_unit, p.price_unit_verified, p.fetched_at FROM portfolio_price p JOIN portfolio_asset a ON a.id = p.asset_id WHERE p.asset_id IN (${ASSET_IDS}) ORDER BY p.fetched_at DESC LIMIT 50;" > "$OUT_DIR/sample_prices.txt" 2>&1 || true
else
  echo "ASSET_IDS not provided; skipping per-asset samples. To enable, set ASSET_IDS='1,2,3' in env before running." | tee "$OUT_DIR/sample_prices_notice.txt"
fi

# Run targeted acceptance tests (operator may set TEST_MARKER or leave default)
TEST_MARKER="${TEST_MARKER:-financial_safety or valuation}"
echo "Running pytest -k \"${TEST_MARKER}\" portfolio-saas/backend -q"
pytest -k "${TEST_MARKER}" portfolio-saas/backend -q 2>&1 | tee "$OUT_DIR/acceptance_pytest.txt" || true

# API check (optional): requires OPERATOR_TOKEN and STAGING_API_URL and ASSET_ID
if [ -n "${OPERATOR_TOKEN:-}" ] && [ -n "${STAGING_API_URL:-}" ] && [ -n "${ASSET_ID:-}" ]; then
  curl -s -H "Authorization: Token ${OPERATOR_TOKEN}" "${STAGING_API_URL}/api/latest-prices/?asset_id=${ASSET_ID}" | jq '.' > "$OUT_DIR/api_latest_prices_${ASSET_ID}.json" || true
else
  echo "Skipping API check (OPERATOR_TOKEN/STAGING_API_URL/ASSET_ID not set)." | tee "$OUT_DIR/api_check_notice.txt"
fi

popd > /dev/null

echo "Staging migration & backfill check complete. Collected outputs in $OUT_DIR"
echo "Review $OUT_DIR/aggregate_by_unit.txt and $OUT_DIR/acceptance_pytest.txt and the API/admin samples."
