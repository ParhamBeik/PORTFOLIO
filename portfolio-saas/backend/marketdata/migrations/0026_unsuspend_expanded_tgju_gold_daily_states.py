"""Unsuspend GOLD_DAILY ArchiveFetchState rows now covered by expanded TGJU slugs."""

from django.db import migrations

from marketdata.sources.tgju import HISTORY_BRS_SYMBOLS

_OLD_SUSPEND_REASON = "BrsApi Gold_Currency_Pro discontinued; no TGJU history slug"


def unsuspend_expanded_tgju(apps, schema_editor):
    ArchiveFetchState = apps.get_model("marketdata", "ArchiveFetchState")
    ArchiveFetchState.objects.filter(
        endpoint="gold_daily",
        symbol__in=HISTORY_BRS_SYMBOLS,
        last_error=_OLD_SUSPEND_REASON,
    ).update(
        suspended_at=None,
        last_error="",
        next_attempt_at=None,
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("marketdata", "0025_suspend_non_tgju_gold_daily_states"),
    ]

    operations = [
        migrations.RunPython(unsuspend_expanded_tgju, noop_reverse),
    ]
