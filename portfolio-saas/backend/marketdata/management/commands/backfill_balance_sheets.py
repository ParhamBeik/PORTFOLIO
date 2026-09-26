"""Bounded source-backed balance-sheet ingestion for observed Codal V9 filings."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from marketdata.codal_classification import classify_announcement
from marketdata.codal_parsers import ParsedDocument
from marketdata.codal_pipeline import _persist_parsed
from marketdata.codal_statements import (
    balance_sheet_url, parse_balance_sheet, parse_income_statement,
)
from marketdata.codal_storage import (
    _absolute_url, download_artifact, load_artifact, store_artifact,
)
from marketdata.models import CodalAnnouncement, CodalArtifact


class Command(BaseCommand):
    help = "Certify observed V9 balance sheets from archived or explicitly fetched Codal sheets."

    def add_arguments(self, parser):
        parser.add_argument("--symbol", default="", help="Restrict to one TSE symbol.")
        parser.add_argument("--after-id", type=int, default=0, help="Resume after this income artifact ID.")
        parser.add_argument("--limit", type=int, default=20, help="Maximum source filings to inspect (1–100).")
        parser.add_argument("--fetch", action="store_true", help="Fetch missing balance sheets from Codal.")
        parser.add_argument("--dry-run", action="store_true", help="Validate without database or object-store writes.")

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 100 or options["after_id"] < 0:
            raise CommandError("--limit must be 1–100 and --after-id nonnegative")
        qs = CodalArtifact.objects.filter(
            Q(report__category=CodalAnnouncement.Category.STATEMENTS)
            | Q(report__announcement__doc_type="financial_statements"),
            id__gt=options["after_id"], kind=CodalArtifact.Kind.HTML,
            fetch_status=CodalArtifact.FetchStatus.STORED,
        ).select_related("report__announcement").order_by("id")
        if options["symbol"]:
            qs = qs.filter(report__announcement__symbol=options["symbol"])

        inspected = certified = skipped = errors = 0
        last_id = options["after_id"]
        for source_artifact in qs[:options["limit"]]:
            inspected += 1
            last_id = source_artifact.id
            report = source_artifact.report
            announcement = report.announcement
            if source_artifact.source_url != _absolute_url(announcement.link):
                skipped += 1  # A derived sheet is never a new source filing.
                continue
            classified = classify_announcement(announcement)
            if classified["category"] != CodalAnnouncement.Category.STATEMENTS:
                skipped += 1
                continue
            arguments = {
                "symbol": announcement.symbol, "company_name": announcement.company_name,
                "title": announcement.title, "period_end": classified["period_end"],
                "is_consolidated": classified["is_consolidated"],
                "is_audited": classified["is_audited"],
            }
            try:
                income_html = load_artifact(source_artifact)
                if not parse_income_statement(income_html, **arguments):
                    skipped += 1  # The source filing itself is not certified.
                    continue
                url = balance_sheet_url(
                    income_html, source_artifact.source_url,
                    is_consolidated=classified["is_consolidated"],
                )
                if not url:
                    skipped += 1
                    continue
                balance_artifact = CodalArtifact.objects.filter(
                    report=report, kind=CodalArtifact.Kind.HTML, source_url=url,
                ).first()
                if (balance_artifact and balance_artifact.fetch_status == CodalArtifact.FetchStatus.STORED
                        and balance_artifact.extractions.filter(
                            checksum_sha256=balance_artifact.checksum_sha256,
                            parser_version=settings.CODAL_STATEMENT_PARSER_VERSION,
                        ).exists()):
                    skipped += 1
                    continue
                if balance_artifact and balance_artifact.fetch_status == CodalArtifact.FetchStatus.STORED:
                    balance_html = load_artifact(balance_artifact)
                elif options["fetch"]:
                    _, content_type, balance_html = download_artifact(url, CodalArtifact.Kind.HTML)
                    if not options["dry_run"]:
                        key, checksum = store_artifact(balance_html, content_type, CodalArtifact.Kind.HTML)
                        if balance_artifact is None:
                            balance_artifact = CodalArtifact(report=report, kind=CodalArtifact.Kind.HTML, source_url=url)
                        balance_artifact.s3_key = key
                        balance_artifact.checksum_sha256 = checksum
                        balance_artifact.content_type = content_type
                        balance_artifact.size_bytes = len(balance_html)
                        balance_artifact.fetch_status = CodalArtifact.FetchStatus.STORED
                        balance_artifact.error_code = ""
                        balance_artifact.save()
                else:
                    skipped += 1
                    continue
                facts = parse_balance_sheet(balance_html, **arguments)
                if not facts:
                    skipped += 1
                    continue
                if not options["dry_run"]:
                    for field, value in classified.items():
                        setattr(report, field, value)
                    report.parser_version = settings.CODAL_STATEMENT_PARSER_VERSION
                    _persist_parsed(report, balance_artifact, ParsedDocument(facts=facts))
                    report.save()
                certified += 1
            except Exception as exc:
                errors += 1
                self.stderr.write(f"source artifact {source_artifact.id}: {type(exc).__name__}")
        self.stdout.write(
            f"inspected={inspected} certified={certified} skipped={skipped} "
            f"errors={errors} last_artifact_id={last_id} "
            f"fetch={options['fetch']} dry_run={options['dry_run']}"
        )
        if errors:
            raise CommandError(f"{errors} artifact(s) failed; inspect and rerun the same range")
