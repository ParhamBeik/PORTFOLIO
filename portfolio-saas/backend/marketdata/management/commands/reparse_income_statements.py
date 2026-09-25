"""Certify supported income sheets from immutable archived Codal documents."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from marketdata.codal_classification import classify_announcement
from marketdata.codal_parsers import ParsedDocument
from marketdata.codal_pipeline import _persist_parsed
from marketdata.codal_statements import parse_income_statement
from marketdata.codal_storage import load_artifact
from marketdata.models import CodalAnnouncement, CodalArtifact, CodalReport, CodalVerification


class Command(BaseCommand):
    help = "Offline, append-only certification of known V9 income statements."

    def add_arguments(self, parser):
        parser.add_argument("--symbol", default="", help="Restrict to one TSE symbol.")
        parser.add_argument("--after-id", type=int, default=0, help="Resume after this artifact ID.")
        parser.add_argument("--limit", type=int, default=100, help="Maximum artifacts to inspect (1–1000).")
        parser.add_argument("--dry-run", action="store_true", help="Parse without writing.")

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 1000:
            raise CommandError("--limit must be between 1 and 1000")
        if options["after_id"] < 0:
            raise CommandError("--after-id must be nonnegative")
        qs = CodalArtifact.objects.filter(
            Q(report__category=CodalAnnouncement.Category.STATEMENTS)
            | Q(report__announcement__doc_type="financial_statements"),
            id__gt=options["after_id"],
            kind__in=[CodalArtifact.Kind.HTML, CodalArtifact.Kind.EXCEL],
            fetch_status=CodalArtifact.FetchStatus.STORED,
        ).select_related("report__announcement").order_by("id")
        if options["symbol"]:
            qs = qs.filter(report__announcement__symbol=options["symbol"])

        inspected = certified = skipped = errors = 0
        last_id = options["after_id"]
        for artifact in qs[:options["limit"]]:
            inspected += 1
            last_id = artifact.id
            report = artifact.report
            if report.extractions.filter(
                artifact=artifact, checksum_sha256=artifact.checksum_sha256,
                parser_version=settings.CODAL_STATEMENT_PARSER_VERSION,
            ).exists():
                skipped += 1
                continue
            classified = classify_announcement(report.announcement)
            if classified["category"] != CodalAnnouncement.Category.STATEMENTS:
                skipped += 1
                continue
            try:
                facts = parse_income_statement(
                    load_artifact(artifact), symbol=report.announcement.symbol,
                    company_name=report.announcement.company_name,
                    title=report.announcement.title,
                    period_end=classified["period_end"],
                    is_consolidated=classified["is_consolidated"],
                    is_audited=classified["is_audited"],
                )
                if not facts:
                    skipped += 1
                    continue
                if not options["dry_run"]:
                    for field, value in classified.items():
                        setattr(report, field, value)
                    report.parser_version = settings.CODAL_STATEMENT_PARSER_VERSION
                    _persist_parsed(report, artifact, ParsedDocument(facts=facts))
                    report.status = CodalReport.Status.PARSED
                    report.quality = CodalReport.Quality.DEGRADED
                    report.verification_status = CodalVerification.EXTRACTED
                    report.error_code = ""
                    report.save()
                certified += 1
            except Exception as exc:
                errors += 1
                self.stderr.write(f"artifact {artifact.id}: {type(exc).__name__}")
        self.stdout.write(
            f"inspected={inspected} certified={certified} skipped={skipped} "
            f"errors={errors} last_artifact_id={last_id} dry_run={options['dry_run']}"
        )
        if errors:
            raise CommandError(f"{errors} artifact(s) failed; inspect and rerun the same range")
