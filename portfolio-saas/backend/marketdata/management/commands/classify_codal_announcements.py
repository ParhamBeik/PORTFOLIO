"""Label announcements from title or preserved provider category.

Zero API calls: this only reads title and source_category through the pure
marketdata.codal_classification.classify() function; it never touches a PDF/
Excel link. Idempotent -- classify() is a deterministic function of columns
already on the row, so re-running (e.g. after a rule change) converges to the
same state and only rewrites rows whose verdict actually changed. Historic
category values may have been overwritten by extraction; they are not source.
"""
import collections

from django.core.management.base import BaseCommand

from marketdata.codal_classification import classify
from marketdata.models import CodalAnnouncement

BATCH_SIZE = 2000


class Command(BaseCommand):
    help = (
        "Label existing CodalAnnouncement rows with doc_type/tier/classified_by "
        "using marketdata.codal_classification.classify(). Zero API calls."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Compute and report counts without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        # Short text/int columns fit in memory at the present warehouse size.
        # within one process's memory, so this loads once rather than paying
        # for a server-side cursor that then has to interleave with writes.
        rows = list(
            CodalAnnouncement.objects.only(
                "id", "title", "source_category", "source_category_title",
                "doc_type", "tier", "classified_by",
            )
        )

        tier_counts = collections.Counter()
        doc_type_counts = collections.Counter()
        source_counts = collections.Counter()
        to_write = []

        for row in rows:
            result = classify(row.title, row.source_category, row.source_category_title)
            tier_counts[result.tier] += 1
            doc_type_counts[result.doc_type] += 1
            source_counts[result.classified_by] += 1
            if (row.doc_type, row.tier, row.classified_by) == (
                result.doc_type, result.tier, result.classified_by,
            ):
                continue
            row.doc_type = result.doc_type
            row.tier = result.tier
            row.classified_by = result.classified_by
            to_write.append(row)

        if not dry_run:
            for start in range(0, len(to_write), BATCH_SIZE):
                CodalAnnouncement.objects.bulk_update(
                    to_write[start:start + BATCH_SIZE],
                    ["doc_type", "tier", "classified_by"],
                )

        verb = "Would change" if dry_run else "Changed"
        self.stdout.write(f"{verb} {len(to_write)} / {len(rows)} rows.")
        self.stdout.write("Tier distribution (full table, post-classification):")
        for tier in sorted(tier_counts):
            self.stdout.write(f"  tier {tier}: {tier_counts[tier]}")
        self.stdout.write("doc_type distribution:")
        for doc_type, n in doc_type_counts.most_common():
            self.stdout.write(f"  {doc_type:28} {n}")
        self.stdout.write("classified_by:")
        for source, n in source_counts.most_common():
            self.stdout.write(f"  {source:10} {n}")
