"""Exercise the 0041 dedup migration's forwards()/backwards() functions
directly against seeded rows, mirroring the exact patterns confirmed against
production data before this migration was written: a clean one-correct-one-
wrong-ts pair, a nullable-field NULL-vs-value case, and a rare NOT-NULL
price disagreement where the correct-ts row's own value must survive."""
import importlib
from datetime import timedelta

import pytest
from django.db import connection

from marketdata import jalali

pytestmark = pytest.mark.django_db


def _load_migration():
    return importlib.import_module(
        "marketdata.migrations.0041_deduplicate_dailystockhistory_ts_drift"
    )


def test_dedup_merges_nullable_fields_and_keeps_correct_row():
    from marketdata.models import DailyStockHistory

    mod = _load_migration()
    date = "1404-01-01"
    correct_ts = jalali.to_datetime(date)
    wrong_ts = correct_ts + timedelta(hours=7)

    wrong = DailyStockHistory.objects.create(
        symbol="کاما", date=date, ts=wrong_ts, pl=1000, pc=1000,
        buy_count_i=42, buy_i_volume=500,
    )
    correct = DailyStockHistory.objects.create(
        symbol="کاما", date=date, ts=correct_ts, pl=1000, pc=1000,
        buy_count_i=None,
    )

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT to_regclass('{mod.BACKUP_TABLE}')")

    class _FakeSchemaEditor:
        pass

    _FakeSchemaEditor.connection = connection
    mod.forwards(None, _FakeSchemaEditor)

    assert DailyStockHistory.objects.filter(symbol="کاما", date=date).count() == 1
    survivor = DailyStockHistory.objects.get(symbol="کاما", date=date)
    assert survivor.id == correct.id
    assert survivor.ts == correct_ts
    # Nullable field backfilled from the deleted row.
    assert survivor.buy_count_i == 42
    assert survivor.buy_i_volume == 500

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {mod.BACKUP_TABLE}")
        assert cursor.fetchone()[0] == 2

    mod.backwards(None, _FakeSchemaEditor)

    rows = DailyStockHistory.objects.filter(symbol="کاما", date=date).order_by("id")
    assert rows.count() == 2
    restored_correct = rows.get(id=correct.id)
    assert restored_correct.buy_count_i is None


def test_dedup_keeps_correct_rows_own_price_on_disagreement():
    """A rare NOT-NULL price disagreement: the surviving (correct-ts) row's
    own value must be kept, never overwritten from the dropped row."""
    from marketdata.models import DailyStockHistory

    mod = _load_migration()
    date = "1403-06-15"
    correct_ts = jalali.to_datetime(date)
    wrong_ts = correct_ts + timedelta(hours=7)

    DailyStockHistory.objects.create(symbol="فملی", date=date, ts=wrong_ts, pl=9240)
    correct = DailyStockHistory.objects.create(symbol="فملی", date=date, ts=correct_ts, pl=9230)

    class _FakeSchemaEditor:
        pass

    _FakeSchemaEditor.connection = connection
    mod.forwards(None, _FakeSchemaEditor)

    survivor = DailyStockHistory.objects.get(symbol="فملی", date=date)
    assert survivor.id == correct.id
    assert survivor.pl == 9230


def test_dedup_leaves_non_duplicate_rows_untouched():
    from marketdata.models import DailyStockHistory

    mod = _load_migration()
    DailyStockHistory.objects.create(symbol="تفیرو", date="1402-01-01", pl=500)

    class _FakeSchemaEditor:
        pass

    _FakeSchemaEditor.connection = connection
    mod.forwards(None, _FakeSchemaEditor)

    assert DailyStockHistory.objects.filter(symbol="تفیرو").count() == 1


def test_dedup_aborts_on_unexpected_pattern():
    """If neither (or both) rows in a pair match the current ts formula, the
    migration must refuse to guess and raise instead of silently deleting."""
    from marketdata.models import DailyStockHistory

    mod = _load_migration()
    date = "1401-01-01"
    correct_ts = jalali.to_datetime(date)
    # Both rows wrong -- zero matches, the unverified case.
    DailyStockHistory.objects.create(
        symbol="بزاگرس", date=date, ts=correct_ts + timedelta(hours=1), pl=1,
    )
    DailyStockHistory.objects.create(
        symbol="بزاگرس", date=date, ts=correct_ts + timedelta(hours=2), pl=1,
    )

    class _FakeSchemaEditor:
        pass

    _FakeSchemaEditor.connection = connection
    with pytest.raises(RuntimeError):
        mod.forwards(None, _FakeSchemaEditor)
