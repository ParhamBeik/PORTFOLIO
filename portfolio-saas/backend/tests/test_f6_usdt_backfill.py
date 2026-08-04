"""Tests for F6 USDT_IRT historical correction."""
import csv
import hashlib
from decimal import Decimal
from io import StringIO
from unittest.mock import patch, MagicMock

import pytest
from django.core.management import call_command
from django.db import connection
from django.core.management.base import CommandError, BaseCommand
from django.test import TransactionTestCase


@pytest.mark.django_db(transaction=True)
class TestF6USDTBackfill(TransactionTestCase):
    """Test the USDT_IRT correction command."""

    def setUp(self):
        """Create test data: 5 confirmed 10x rows, 1 ambiguous, 1 post-cutoff, 1 provider-gap."""
        self.expected_count_patcher = patch("marketdata.management.commands.fix_usdt_irt_history.EXPECTED_COUNT", 5)
        self.expected_count_patcher.start()

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

    def tearDown(self):
        """Clean up the patched EXPECTED_COUNT."""
        self.expected_count_patcher.stop()

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
        self._run_generate_manifest(mock_fetch)

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
            for date, expected in [
                ("1402-08-19", 52073.0),
                ("1402-08-20", 51792.0),
                ("1402-08-21", 52400.0),
                ("1402-08-22", 51950.0),
                ("1402-08-23", 51598.0),
            ]:
                cursor.execute("SELECT close_price FROM marketdata_goldcurrencyhistory WHERE symbol = 'USDT_IRT' AND date = %s", [date])
                val = cursor.fetchone()[0]
                assert float(val) == pytest.approx(expected)

            # Ambiguous: unchanged
            cursor.execute("SELECT close_price FROM marketdata_goldcurrencyhistory WHERE symbol = 'USDT_IRT' AND date = '1402-10-25'")
            assert float(cursor.fetchone()[0]) == 5351.2

            # Provider gap: unchanged
            cursor.execute("SELECT close_price FROM marketdata_goldcurrencyhistory WHERE symbol = 'USDT_IRT' AND date = '1402-08-09'")
            assert float(cursor.fetchone()[0]) == 5207.3

            # Post-cutoff: unchanged
            cursor.execute("SELECT close_price FROM marketdata_goldcurrencyhistory WHERE symbol = 'USDT_IRT' AND date = '1405-05-11'")
            assert float(cursor.fetchone()[0]) == 192802.0

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

    @patch("marketdata.management.commands.fix_usdt_irt_history.Command._fetch_provider_data")
    def test_disputed_rows_included(self, mock_fetch):
        """Verify the 5 disputed rows are included under the 5% tolerance check."""
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM marketdata_goldcurrencyhistory WHERE symbol = 'USDT_IRT'")
            
            disputed_data = [
                ("1402-11-04", [5558.1, 5609.7, 5378.5, 5565.9], [55581.0, 56097.0, 55411.0, 55534.0]),
                ("1402-11-05", [5553.4, 5600.0, 5541.6, 5574.4], [53785.0, 56000.0, 53785.0, 55561.0]),
                ("1402-11-16", [5523.1, 5551.9, 5517.9, 5539.1], [55231.0, 55665.0, 54632.0, 55312.0]),
                ("1402-12-07", [5732.6, 5799.7, 5716.1, 5763.5], [57326.0, 58000.0, 55900.0, 57936.0]),
                ("1405-05-04", [18772.2, 18848.9, 18700.1, 18732.4], [187722.0, 189217.0, 183868.0, 188534.0])
            ]
            
            provider_mock = {}
            for date, stored_ohlc, provider_ohlc in disputed_data:
                cursor.execute("""
                    INSERT INTO marketdata_goldcurrencyhistory
                    (symbol, name, unit, date, open_price, high_price, low_price, close_price)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, ["USDT_IRT", "تتر", "تومان", date,
                      stored_ohlc[0], stored_ohlc[1], stored_ohlc[2], stored_ohlc[3]])
                
                provider_mock[date] = {
                    "open": provider_ohlc[0],
                    "high": provider_ohlc[1],
                    "low": provider_ohlc[2],
                    "close": provider_ohlc[3]
                }
                
        mock_fetch.return_value = provider_mock
        
        # We expect exactly 5 rows in the manifest
        out = StringIO()
        call_command("fix_usdt_irt_history", "--generate-manifest", stdout=out)
        
        with open("/tmp/usdt_irt_manifest.csv", "r") as f:
            reader = csv.DictReader(f)
            dates = [row["date"] for row in reader]
            
        assert len(dates) == 5
        for date, _, _ in disputed_data:
            assert date in dates

    @patch("marketdata.management.commands.fix_usdt_irt_history.Command._fetch_provider_data")
    def test_row_outside_tolerance_excluded(self, mock_fetch):
        """Verify that a row with any field outside the 5% tolerance is excluded."""
        with patch("marketdata.management.commands.fix_usdt_irt_history.EXPECTED_COUNT", 0):
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM marketdata_goldcurrencyhistory WHERE symbol = 'USDT_IRT'")
                # ratio for open is 5000/60000 = 0.083 (< 0.095)
                cursor.execute("""
                    INSERT INTO marketdata_goldcurrencyhistory
                    (symbol, name, unit, date, open_price, high_price, low_price, close_price)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, ["USDT_IRT", "تتر", "تومان", "1402-08-20", 5000.0, 6000.0, 6000.0, 6000.0])
                
            mock_fetch.return_value = {
                "1402-08-20": {"open": 60000.0, "high": 60000.0, "low": 60000.0, "close": 60000.0}
            }
            
            out = StringIO()
            call_command("fix_usdt_irt_history", "--generate-manifest", stdout=out)
            
            with open("/tmp/usdt_irt_manifest.csv", "r") as f:
                reader = csv.DictReader(f)
                dates = [row["date"] for row in reader]
                
            assert len(dates) == 0

    def test_expected_count_is_992_in_audit(self):
        """Verify the Command expected count is exactly 992."""
        self.expected_count_patcher.stop()
        try:
            from marketdata.management.commands.fix_usdt_irt_history import EXPECTED_COUNT
            assert EXPECTED_COUNT == 992
        finally:
            self.expected_count_patcher.start()

    @patch("marketdata.management.commands.fix_usdt_irt_history.Command._fetch_provider_data")
    def test_ambiguous_dates_remain_excluded(self, mock_fetch):
        """Verify that the two ambiguous dates are excluded from the manifest."""
        with patch("marketdata.management.commands.fix_usdt_irt_history.EXPECTED_COUNT", 0):
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM marketdata_goldcurrencyhistory WHERE symbol = 'USDT_IRT'")
                for date in ["1402-10-25", "1405-05-03"]:
                    cursor.execute("""
                        INSERT INTO marketdata_goldcurrencyhistory
                        (symbol, name, unit, date, open_price, high_price, low_price, close_price)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, ["USDT_IRT", "تتر", "تومان", date, 5000.0, 5000.0, 5000.0, 5000.0])
                    
            mock_fetch.return_value = {
                "1402-10-25": {"open": 50000.0, "high": 50000.0, "low": 50000.0, "close": 50000.0},
                "1405-05-03": {"open": 50000.0, "high": 50000.0, "low": 50000.0, "close": 50000.0}
            }
            
            out = StringIO()
            call_command("fix_usdt_irt_history", "--generate-manifest", stdout=out)
            
            with open("/tmp/usdt_irt_manifest.csv", "r") as f:
                reader = csv.DictReader(f)
                dates = [row["date"] for row in reader]
                
            assert len(dates) == 0

    @patch("marketdata.management.commands.fix_usdt_irt_history.Command._fetch_provider_data")
    def test_gap_and_cutoff_excluded(self, mock_fetch):
        """Verify that provider-gap and post-cutoff rows are excluded from the manifest."""
        mock_fetch.return_value = self._get_provider_mock_data()
        
        out = StringIO()
        call_command("fix_usdt_irt_history", "--generate-manifest", stdout=out)
        
        with open("/tmp/usdt_irt_manifest.csv", "r") as f:
            reader = csv.DictReader(f)
            dates = [row["date"] for row in reader]
            
        assert "1402-08-09" not in dates  # Provider-gap
        assert "1405-05-11" not in dates  # Post-cutoff

    @patch("marketdata.management.commands.fix_usdt_irt_history.Command._fetch_provider_data")
    def test_apply_fails_without_or_wrong_hash(self, mock_fetch):
        """Verify that apply mode fails if hash is missing, wrong, or DB values mismatch."""
        mock_fetch.return_value = self._get_provider_mock_data()
        
        # Generate manifest
        self._run_generate_manifest(mock_fetch)
        
        # 1. Missing hash
        with pytest.raises(CommandError) as exc:
            call_command("fix_usdt_irt_history", "--apply")
        assert "manifest-hash is required" in str(exc.value)
        
        # 2. Wrong hash
        with pytest.raises(CommandError) as exc:
            call_command("fix_usdt_irt_history", "--apply", "--manifest-hash=wronghash123")
        assert "Manifest hash mismatch" in str(exc.value)
        
        # 3. DB value mismatch (modifying a row in DB before applying)
        with open("/tmp/usdt_irt_manifest.csv", "rb") as f:
            correct_hash = hashlib.sha256(f.read()).hexdigest()
            
        with connection.cursor() as cursor:
            # Change value of a row to break the immutable before-value check
            cursor.execute("UPDATE marketdata_goldcurrencyhistory SET close_price = 999.9 WHERE date = '1402-08-19'")
            
        with pytest.raises(CommandError) as exc:
            call_command("fix_usdt_irt_history", "--apply", f"--manifest-hash={correct_hash}")
        assert "unexpected current values" in str(exc.value)