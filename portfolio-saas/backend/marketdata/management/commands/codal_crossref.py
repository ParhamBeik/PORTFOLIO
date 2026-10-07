"""Read-only: compare codal.ir letters (CodalLetter) with BrsApi rows (CodalAnnouncement).

Phase 1's deliverable (docs/CODAL-DIRECT-MIGRATION.md §7). For each complete
crawled day it reports how many codal.ir letters match a stored announcement by
LetterSerial, by the old (symbol, code, date, time) key, or not at all -- split
into catalog symbols and other publishers -- plus stored rows codal.ir no longer
lists and field disagreements on matched pairs. Writes nothing.

    python manage.py codal_crossref                 # every complete day
    python manage.py codal_crossref --since 1404-07-01
"""
import re
from collections import Counter
from urllib.parse import unquote

from django.core.management.base import BaseCommand

from marketdata.models import CodalAnnouncement, CodalDiscoveryDay, CodalLetter, MarketInstrument

_SERIAL = re.compile(r"[?&](?:LetterSerial|hs)=([^&]+)", re.I)


def announcement_serial(link, link_pdf=""):
    """The LetterSerial a BrsApi row points at, decoded with unquote (never parse_qsl)."""
    for value in (link, link_pdf):
        match = _SERIAL.search(value or "")
        if match:
            return unquote(match.group(1))
    return ""


def crossref_day(day, catalog):
    ours = list(
        CodalAnnouncement.objects.filter(date_publish=day)
        .values("id", "symbol", "code", "time_publish", "link", "link_pdf")
    )
    by_serial = {announcement_serial(o["link"], o["link_pdf"]): o for o in ours}
    by_key = {(o["symbol"], o["code"], o["time_publish"]): o for o in ours}
    stats, disagree, matched = Counter(), Counter(), set()
    for letter in CodalLetter.objects.filter(date_publish=day).values(
        "symbol", "letter_code", "time_publish", "letter_serial"
    ):
        stats["theirs"] += 1
        found = by_serial.get(letter["letter_serial"]) if letter["letter_serial"] else None
        how = "serial"
        if found is None:
            found = by_key.get((letter["symbol"], letter["letter_code"], letter["time_publish"]))
            how = "key"
        if found is None:
            stats["new_catalog" if letter["symbol"] in catalog else "new_other"] += 1
            continue
        stats[f"matched_{how}"] += 1
        matched.add(found["id"])
        for field, ours_field in (("symbol", "symbol"), ("letter_code", "code"),
                                  ("time_publish", "time_publish")):
            if letter[field] != found[ours_field]:
                disagree[field] += 1
    stats["ours"] = len(ours)
    stats["ours_only"] = len(ours) - len(matched)
    return stats, disagree


class Command(BaseCommand):
    help = "Read-only cross-reference of codal.ir letters against stored announcements."

    def add_arguments(self, parser):
        parser.add_argument("--since", default="", help="Only days on or after this Jalali date.")

    def handle(self, *args, **options):
        days = CodalDiscoveryDay.objects.filter(verified_complete=True)
        if options["since"]:
            days = days.filter(date__gte=options["since"])
        catalog = set(MarketInstrument.objects.values_list("symbol", flat=True))
        totals, disagreements = Counter(), Counter()
        for day in days.order_by("-date").values_list("date", flat=True):
            stats, disagree = crossref_day(day, catalog)
            totals.update(stats)
            disagreements.update(disagree)
            self.stdout.write(f"{day} {dict(stats)}" + (f" disagree={dict(disagree)}" if disagree else ""))
        self.stdout.write(f"TOTAL {dict(totals)} disagree={dict(disagreements)}")
