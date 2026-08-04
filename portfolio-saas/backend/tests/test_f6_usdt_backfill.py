"""Tests for F6 USDT_IRT historical correction."""
import csv
import hashlib
from decimal import Decimal
from io import StringIO
from unittest.mock import patch, MagicMock

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase


@pytest.mark.django_db(transaction=True)
class TestF6USDTBackfill(TransactionTestCase):
    """Test the USDT_IRT correction command."""

    def setUp(self):
        """Create test data: 5 confirmed 10x rows, 1 ambiguous, 1 post-cutoff, 1 provider-gap."""
        with connection.cursor() as cursor:
            # Clear any existing USDT_IRT rows
            cursor.execute("DELETE FROM marketdata_goldcurrencyhistory WHERE symbol = 'USDT_IRT'")

            # 5 confirmed 10x rows (pre-cutoff, all OHLC ~0.1 ratio)
            for i, (date, stored_close, provider_close) in enumerate([
                ("1402-08-19", 5207.3, 52073.0),
                ("1402-08-20", 5179.2, 51792.0),
                ("1402-08-21", 5240.0, 52400.0),
                ("1402-08-22", 5195.0, 51950.0),
                ("1402-08-23", 5159.8, 51598.0),
            ]):
                cursor.execute("""
                    INSERT INTO marketdata_goldcurrencyhistory
                    (symbol, name, unit, date, open_price, high_price, low_price, close_price)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, [
                    "USDT_IRT", "تتر", "تومان", date,
                    stored_close, stored_close, stored_close, stored_close
                ])

            # 1 ambiguous row (ratio ~0.101)
            cursor.execute("""
                INSERT INTO marketdata_goldcurrencyhistory
                (symbol, name, unit, date, open_price, high_price, low_price, close_price)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, ["USDT_IRT", "تتر", "تومان", "1402-10-25", 5351.2, 5351.2, 5351.2, 5351.2])

            # 1 provider-gap row (early date before provider history)
            cursor.execute("""
                INSERT INTO marketdata_goldcurrencyhistory
                (symbol, name, unit, date, open_price, high_price, low_price, close_price)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, ["USDT_IRT", "تتر", "تومان", "1402-08-09", 5207.3, 5207.3, 5207.3, 5207.3])

            # 1 post-cutoff row (correct)
            cursor.execute("""
                INSERT INTO marketdata_goldcurrencyhistory
                (symbol, name, unit, date, open_price, high_price, low_price, close_price)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, ["USDT_IRT", "تتر", "تومان", "1405-05-11", 192802.0, 192802.0, 192802.0, 192802.0])

    def _get_provider_mock_data(self):
        """Mock provider data matching test fixtures."""
        return {
            "1402-08-19": {"open": 52073.0, "high": 52073.0, "low": 52073.0, "close": 52073.0},
            "1402-08-20": {"open": 51792.0, "high": 51792.0, "low": 51792.0, "close": 51792.0},
            "1402-08-21": {"open": 52400.0, "high": 52400.0, "low": 52400.0, "close": 52400.0},
            "1402-08-22": {"open": 51950.0, "high": 51950.0, "low": 51950.0, "close": 51950.0},
            "1402-08-23": {"open": 51598.0, "high": 51598.0, "low": 51598.0, "close": 51598.0},
            "1402-10-25": {"open": 52941.0, "high": 52941.0, "low": 52941.0, "close": 52941.0},
            "1405-05-11": {"open": 192298.0, "high": 192298.0, "low": 192298.0, "close": 192298.0},
        }

    @patch("marketdata.management.commands.fix_usdt_irt_history.Command._fetch_provider_data")
    def test_dry_run_makes_no_changes(self, mock_fetch):
        """Dry-run must not modify any database rows."""
        mock_fetch.return_value = self._get_provider_mock_data()

        # Capture initial state
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM marketdata_goldcurrencyhistory WHERE symbol = 'USDT_IRT'")
            initial_count = cursor.fetchone()[0]

        out = StringIO()
        call_command("fix_usdt_irt_history", "--dry-run", stdout=out)

        # Verify no changes
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM marketdata_goldcurrencyhistory WHERE symbol = 'USDT_IRT'")
            final_count = cursor.fetchone()[0]

        assert final_count == initial_count
        assert "Would update 5 rows" in out.getvalue()

    @patch("marketdata.management.commands.fix_usdt_irt_history.Command._fetch_provider_data")
    def test_apply_changes_only_confirmed_rows(self, mock_fetch):
        """Apply must update only the 5 confirmed rows, not ambiguous/gap/post-cutoff."""
        mock_fetch.return_value = self._get_provider_mock_data()

        # Generate manifest first
        out = StringIO()
        call_command("fix_usdt_irt_history", "--generate-manifest", stdout=out)
        assert "Rows: 5" in out.getvalue()

        # Load manifest hash
        with open("/tmp/usdt_irt_manifest.csv", "rb") as f:
            manifest_hash = hashlib.sha256(f.read()).hexdigest()

        # Apply with hash
        out = StringIO()
        call_command("fix_usdt_irt_history", "--apply", f"--manifest-hash={manifest_hash}", stdout=out)

        assert "SUCCESS: Updated 5 rows" in out.getvalue()

        # Verify only confirmed rows changed
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT date, close_price FROM marketdata_goldcurrencyhistory
                WHERE symbol = 'USDT_IRT' ORDER BY date
            """)
            rows = cursor.fetchall()

        # 5 confirmed: should be ×10
        for date, close in rows[:5]:
            assert close == pytest.approx(52073.0 if "08-19" in date else
                                          51792.0 if "08-20" in date else
                                          52400.0 if "08-21" in date else
                                          51950.0 if "08-22" in date else
                                          51598.0)

        # Ambiguous: unchanged
        assert rows[5][1] == 5351.2

        # Provider gap: unchanged
        assert rows[6][1] == 5207.3

        # Post-cutoff: unchanged
        assert rows[7][1] == 192802.0

    @patch("marketdata.management.commands.fix_usdt_irt_history.Command._fetch_provider_data")
    def test_correct_rows_not_changed(self, mock_fetch):
        """Rows that are already correct (post-cutoff) must not be modified."""
        mock_fetch.return_value = self._get_provider_mock_data()

        manifest_hash = self._run_generate_manifest(mock_fetch)
        call_command("fix_usdt_irt_history", "--apply", f"--manifest-hash={manifest_hash}")

        # Post-cutoff row should remain at ~192k, not become ~1.9M
        with connection.cursor() as cursor:
            cursor.execute("SELECT close_price FROM marketdata_goldcurrencyhistory WHERE symbol = 'USDT_IRT' AND date = '1405-05-11'")
            close = cursor.fetchone()[0]
            assert close == 192802.0

    @patch("marketdata.management.commands.fix_usdt_irt_history.Command._fetch_provider_data")
    def test_ambiguous_rows_refused(self, mock_fetch):
        """Ambiguous rows (ratio ~0.101) must be excluded from manifest."""
        mock_fetch.return_value = self._get_provider_mock_data()

        manifest_hash = self._run_generate_manifest(mock_fetch)

        # Load manifest and verify ambiguous date not present
        with open("/tmp/usdt_irt_manifest.csv", "r") as f:
            reader = csv.DictReader(f)
            dates = [row["date"] for row in reader]

        assert "1402-10-25" not in dates
        assert "1405-05-03" not in dates
        assert len(dates) == 5  # Only the 5 confirmed

    @patch("marketdata.management.commands.fix_usdt_irt_history.Command._fetch_provider_data")
    def test_idempotent_apply(self, mock_fetch):
        """Running apply twice must make zero changes the second time."""
        mock_fetch.return_value = self._get_provider_mock_data()

        manifest_hash = self._run_generate_manifest(mock_fetch)

        # First apply
        out1 = StringIO()
        call_command("fix_usdt_irt_history", "--apply", f"--manifest-hash={manifest_hash}", stdout=out1)
        assert "Updated 5 rows" in out1.getvalue()

        # Second apply
        out2 = StringIO()
        call_command("fix_usdt_irt_history", "--apply", f"--manifest-hash={manifest_hash}", stdout=out2)
        assert "Updated 0 rows" in out2.getvalue() or "already match" in out2.getvalue()

    @patch("marketdata.management.commands.fix_usdt_irt_history.Command._fetch_provider_data")
    def test_all_ohlc_fields_corrected(self, mock_fetch):
        """All four OHLC fields must be multiplied by 10 consistently."""
        mock_fetch.return_value = self._get_provider_mock_data()

        manifest_hash = self._run_generate_manifest(mock_fetch)
        call_command("fix_usdt_irt_history", "--apply", f"--manifest-hash={manifest_hash}")

        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT open_price, high_price, low_price, close_price
                FROM marketdata_goldcurrencyhistory
                WHERE symbol = 'USDT_IRT' AND date = '1402-08-19'
            """)
            open_p, high_p, low_p, close_p = cursor.fetchone()

        # All four should be ~10x original
        assert open_p == 52073.0
        assert high_p == 52073.0
        assert low_p == 52073.0
        assert close_p == 52073.0

    @patch("marketdata.management.commands.fix_usdt_irt_history.Command._fetch_provider_data")
    def test_rollback_restores_original_values(self, mock_fetch):
        """Rollback snapshot must contain exact before-values for all target rows."""
        mock_fetch.return_value = self._get_provider_mock_data()

        manifest_hash = self._run_generate_manifest(mock_fetch)

        # Capture rollback snapshot path from output
        out = StringIO()
        call_command("fix_usdt_irt_history", "--apply", f"--manifest-hash={manifest_hash}", stdout=out)

        # Find snapshot path
        snapshot_path = None
        for line in out.getvalue().split("\n"):
            if "Rollback snapshot:" in line:
                snapshot_path = line.split(": ")[1].split(" (")[0]
                break

        assert snapshot_path is not None

        # Verify snapshot contains before-values
        with open(snapshot_path, "r") as f:
            reader = csv.DictReader(f)
            snapshot_rows = list(reader)

        assert len(snapshot_rows) == 5
        for row in snapshot_rows:
            # These are the BEFORE values (before ×10)
            date = row["date"]
            if "08-19" in date:
                expected = 5207.3
            elif "08-20" in date:
                expected = 5179.2
            elif "08-21" in date:
                expected = 5240.0
            elif "08-22" in date:
                expected = 5195.0
            elif "08-23" in date:
                expected = 5159.8
            else:
                expected = None
            assert expected is not None
            assert float(row["close_price"]) == pytest.approx(expected)

        # Verify SHA-256 can be computed
        with open(snapshot_path, "rb") as f:
            sha256 = hashlib.sha256(f.read()).hexdigest()
        assert len(sha256) == 64

    @patch("marketdata.management.commands.fix_usdt_irt_history.Command._fetch_provider_data")
    def test_current_ingestion_leaves_toman_unchanged(self, mock_fetch):
        """Current ingestion path for USDT_IRT must not apply any conversion."""
        from marketdata.currency import to_toman

        mock_fetch.return_value = self._get_provider_mock_data()

        # Simulate current ingestion: provider sends 'تومان', unit='تومان'
        result = to_toman("USDT_IRT", 192787, "تومان")
        assert result == Decimal("192787")  # No conversion

        # Provider sends 'تومان' (already Toman), should pass through
        result = to_toman("USDT", 192787, "تومان")
        assert result == Decimal("192787")

    def _run_generate_manifest(self, mock_fetch):
        """Helper to generate manifest and return hash."""
        mock_fetch.return_value = self._get_provider_mock_data()
        out = StringIO()
        call_command("fix_usdt_irt_history", "--generate-manifest", stdout=out)
        with open("/tmp/usdt_irt_manifest.csv", "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()