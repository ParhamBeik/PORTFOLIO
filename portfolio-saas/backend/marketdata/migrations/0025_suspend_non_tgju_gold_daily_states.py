"""Suspend non-TGJU GOLD_DAILY ArchiveFetchState rows after BrsApi retirement."""

from django.db import migrations
from django.utils import timezone

from marketdata.sources.tgju import HISTORY_BRS_SYMBOLS

_SUSPEND_REASON = "BrsApi Gold_Currency_Pro discontinued; no TGJU history slug"


def suspend_non_tgju_gold_daily(apps, schema_editor):
    ArchiveFetchState = apps.get_model("marketdata", "ArchiveFetchState")
    (
        ArchiveFetchState.objects.filter(
            endpoint="gold_daily",
            suspended_at__isnull=True,
        )
        .exclude(symbol__in=HISTORY_BRS_SYMBOLS)
        .update(
            suspended_at=timezone.now(),
            verified_complete=False,
            last_error=_SUSPEND_REASON,
        )
    )


def unsuspend_non_tgju_gold_daily(apps, schema_editor):
    ArchiveFetchState = apps.get_model("marketdata", "ArchiveFetchState")
    (
        ArchiveFetchState.objects.filter(
            endpoint="gold_daily",
            last_error=_SUSPEND_REASON,
        )
        .exclude(symbol__in=HISTORY_BRS_SYMBOLS)
        .update(
            suspended_at=None,
            last_error="",
        )
    )


class Migration(migrations.Migration):
    dependencies = [
        ("marketdata", "0024_tick_day_summary"),
    ]

    operations = [
        migrations.RunPython(
            suspend_non_tgju_gold_daily,
            unsuspend_non_tgju_gold_daily,
        ),
    ]
