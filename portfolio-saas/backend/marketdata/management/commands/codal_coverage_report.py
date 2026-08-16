"""Read-only coverage report: what stage Codal classification is at, per tier.

Strictly read-only -- there is no --apply. Answers "what do we already hold":
per-tier row/symbol counts, date ranges, the newest document's age, and which
symbols have zero Tier-1 (financial-statement-grade) documents at all.
"""
import json

import jdatetime
from django.core.management.base import BaseCommand
from django.db.models import Count, Max, Min

from marketdata.codal_classification import TIER_1
from marketdata.models import CodalAnnouncement

PREVIEW_LIMIT = 20


def _age_days(jalali_date: str | None) -> int | None:
    if not jalali_date:
        return None
    try:
        year, month, day = (int(p) for p in jalali_date.split("-"))
        return (jdatetime.date.today() - jdatetime.date(year, month, day)).days
    except ValueError:
        return None


class Command(BaseCommand):
    help = "Read-only report on Codal classification coverage, per tier."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", dest="as_json")

    def handle(self, *args, **options):
        qs = CodalAnnouncement.objects.all()
        total_rows = qs.count()
        total_symbols = qs.values("symbol").distinct().count()
        unclassified_rows = qs.filter(tier__isnull=True).count()

        tiers = {}
        for tier in (1, 2, 3):
            agg = qs.filter(tier=tier).aggregate(
                rows=Count("id"),
                symbols=Count("symbol", distinct=True),
                earliest=Min("date_publish"),
                latest=Max("date_publish"),
            )
            tiers[tier] = {**agg, "newest_age_days": _age_days(agg["latest"])}

        tier1_symbols = set(
            qs.filter(tier=TIER_1).values_list("symbol", flat=True).distinct()
        )
        all_symbols = set(qs.values_list("symbol", flat=True).distinct())
        no_tier1 = sorted(all_symbols - tier1_symbols)

        payload = {
            "total_rows": total_rows,
            "total_symbols": total_symbols,
            "unclassified_rows": unclassified_rows,
            "tiers": tiers,
            "symbols_without_tier1": {"count": len(no_tier1), "symbols": no_tier1},
        }

        if options["as_json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        self.stdout.write(f"Total rows: {total_rows}  |  Total symbols: {total_symbols}")
        self.stdout.write(f"Unclassified rows (tier is NULL): {unclassified_rows}")
        self.stdout.write("")
        for tier in (1, 2, 3):
            t = tiers[tier]
            self.stdout.write(
                f"Tier {tier}: {t['rows']} rows, {t['symbols']} symbols, "
                f"range [{t['earliest']} .. {t['latest']}], "
                f"newest is {t['newest_age_days']} days old"
            )
        self.stdout.write("")
        self.stdout.write(f"Symbols with NO Tier-1 documents: {len(no_tier1)} / {total_symbols}")
        if no_tier1:
            preview = ", ".join(no_tier1[:PREVIEW_LIMIT])
            extra = len(no_tier1) - PREVIEW_LIMIT
            suffix = f", ... (+{extra} more)" if extra > 0 else ""
            self.stdout.write(f"  {preview}{suffix}")
