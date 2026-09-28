"""Reparse archived Codal monthly reports without contacting codal.ir."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from marketdata.codal_classification import classify_announcement
from marketdata.codal_parsers import extract_typed_facts, parse_artifact, reconcile_monthly_sales
from marketdata.codal_pipeline import CodalExtractionRegressed, _persist_parsed
from marketdata.codal_storage import load_artifact
from marketdata.models import CodalAnnouncement, CodalArtifact, CodalReport, CodalVerification


class Command(BaseCommand):
    help = "Reparse stored monthly-sales documents with the current parser; append-only and offline."

    def add_arguments(self, parser):
        parser.add_argument("--symbol", default="", help="Restrict to one TSE symbol.")
        parser.add_argument("--after-id", type=int, default=0, help="Resume after this artifact ID.")
        parser.add_argument("--limit", type=int, default=100, help="Maximum artifacts to inspect (1–1000).")
        parser.add_argument("--dry-run", action="store_true", help="Parse and report, without writing.")

    def handle(self, *args, **options):
        limit = options["limit"]
        if not 1 <= limit <= 1000:
            raise CommandError("--limit must be between 1 and 1000")
        if options["after_id"] < 0:
            raise CommandError("--after-id must be nonnegative")
        qs = CodalArtifact.objects.filter(
            id__gt=options["after_id"],
            kind=CodalArtifact.Kind.EXCEL,
            fetch_status=CodalArtifact.FetchStatus.STORED,
            report__category=CodalAnnouncement.Category.PRODUCTION_SALES,
        ).select_related("report__announcement").order_by("id")
        if options["symbol"]:
            qs = qs.filter(report__announcement__symbol=options["symbol"])

        inspected = parsed = reconciled = skipped = errors = 0
        last_id = options["after_id"]
        for artifact in qs[:limit]:
            inspected += 1
            last_id = artifact.id
            report = artifact.report
            if report.extractions.filter(
                artifact=artifact,
                checksum_sha256=artifact.checksum_sha256,
                parser_version=settings.CODAL_PARSER_VERSION,
            ).exists():
                skipped += 1
                continue
            try:
                content = load_artifact(artifact)
                document = parse_artifact(artifact.kind, content)
                classified = classify_announcement(report.announcement)
                if classified["category"] != CodalAnnouncement.Category.PRODUCTION_SALES:
                    skipped += 1
                    continue
                period_end = classified["period_end"]
                extract_typed_facts(document, classified["category"], period_end)
                if not document.facts:
                    skipped += 1
                    continue
                certified = reconcile_monthly_sales(document.facts)
                if not options["dry_run"]:
                    for field, value in classified.items():
                        setattr(report, field, value)
                    report.parser_version = settings.CODAL_PARSER_VERSION
                    _persist_parsed(report, artifact, document)
                    report.status = CodalReport.Status.PARSED
                    report.quality = CodalReport.Quality.DEGRADED
                    report.verification_status = CodalVerification.EXTRACTED
                    report.error_code = ""
                    report.save()
                parsed += 1
                reconciled += certified is not None
            except CodalExtractionRegressed:
                skipped += 1
            except Exception as exc:
                errors += 1
                self.stderr.write(f"artifact {artifact.id}: {type(exc).__name__}")
        self.stdout.write(
            f"inspected={inspected} parsed={parsed} reconciled_totals={reconciled} "
            f"skipped={skipped} errors={errors} last_artifact_id={last_id} "
            f"dry_run={options['dry_run']}"
        )
        if errors:
            raise CommandError(f"{errors} artifact(s) failed; rerun the same range after reviewing them")
