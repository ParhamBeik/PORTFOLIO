"""Orchestrates one Codal announcement's extraction: download every declared
artifact, store what downloads clean, parse the first usable one, extract
typed facts. Ported from the pipeline stripped in commit 2ea22be, storage
swapped to S3/MinIO (codal_storage.py), OCR intentionally left out (see
codal_parsers.py).

Retry policy lives entirely in the CodalReport.status this module chooses,
because that status is exactly what `queue_codal_extractions` (tasks.py)
reads to decide what to requeue:

* BLOCKED_NETWORK / BLOCKED_STORAGE -- transient (a fetch/store failed, not
  the document itself). Retried automatically on the next queue_codal_
  extractions pass.
* FAILED -- an unclassified exception. Also retried; treated as transient
  until proven otherwise, since a new exception type is more likely a bug
  worth re-observing than a permanent dead end.
* UNSUPPORTED -- permanent: every artifact failed its own content check
  (bad magic bytes, disallowed host, oversized, zip bomb), the announcement
  has no artifact links at all, or nothing parseable came out of what did
  download. A network retry cannot fix a document that fails its own
  validation. Never requeued.
* PARSED / NEEDS_REVIEW -- terminal successes (NEEDS_REVIEW means "parsed
  but the category's fact rules found nothing," which is a parser-coverage
  question, not a fetch problem).
"""
from django.db import transaction
from django.utils import timezone

from .codal_classification import classify_announcement
from .codal_parsers import category_reconciles, extract_typed_facts, parse_artifact
from .codal_storage import (
    CodalArtifactRejected,
    CodalBlockedNetwork,
    CodalBlockedStorage,
    _absolute_url,
    download_artifact,
    store_artifact,
)
from .models import (
    CodalArtifact,
    CodalFact,
    CodalParsedTable,
    CodalReport,
    CodalSection,
)
from .quota import ARCHIVE, QuotaExhausted, reserve_request


class CodalExtractionRegressed(RuntimeError):
    """A re-parse produced less than what is already stored."""


def _artifact_sources(announcement):
    return [
        (CodalArtifact.Kind.EXCEL, announcement.link_excel),
        (CodalArtifact.Kind.HTML, announcement.link),
        (CodalArtifact.Kind.PDF, announcement.link_pdf),
        (CodalArtifact.Kind.ATTACHMENT, announcement.link_attachment),
    ]


def _artifact_states(report):
    """Per-artifact outcome, compact enough for one ledger line."""
    return {
        artifact.kind: artifact.error_code or artifact.fetch_status
        for artifact in report.artifacts.all()
    }


def _find_revision(report):
    if not report.is_correction:
        return None
    return (
        CodalReport.objects.filter(
            announcement__symbol=report.announcement.symbol,
            report_type=report.report_type,
            period_end=report.period_end,
        )
        .exclude(pk=report.pk)
        .order_by("-announcement__date_publish", "-announcement__time_publish")
        .first()
    )


def _persist_parsed(report, artifact, parsed):
    # Re-running a report replaces its extraction wholesale, which is right
    # when the new parse is at least as good. It is not right when the new
    # parse yields nothing: that would silently destroy a previous run's good
    # facts and leave no reason behind. A worse result does not get to
    # overwrite a better one.
    if not parsed.facts and report.facts.exists():
        raise CodalExtractionRegressed(
            f"parse produced 0 facts but {report.facts.count()} are already stored"
        )
    with transaction.atomic():
        report.parsed_tables.all().delete()
        report.sections.all().delete()
        report.facts.all().delete()
        tables = []
        for index, table in enumerate(parsed.tables):
            tables.append(CodalParsedTable.objects.create(
                report=report,
                artifact=artifact,
                name=table["name"][:255],
                sheet_name=table["sheet_name"][:255],
                table_index=index,
                headers=table["headers"],
                rows=table["rows"],
                source_coordinates=table["source_coordinates"],
                parser_version=report.parser_version,
            ))
        sections = [
            CodalSection.objects.create(
                report=report,
                artifact=artifact,
                heading=section["heading"][:255],
                body=section["body"],
                section_index=index,
                source_coordinates=section["source_coordinates"],
                confidence=parsed.confidence,
            )
            for index, section in enumerate(parsed.sections)
        ]
        for fact in parsed.facts:
            coordinates = fact["source_coordinates"]
            table = tables[coordinates["table_index"]] if "table_index" in coordinates else None
            section = sections[0] if not table and sections else None
            CodalFact.objects.create(
                report=report,
                table=table,
                section=section,
                parser_version=report.parser_version,
                **fact,
            )


def extract_report(announcement_id):
    from .models import CodalAnnouncement

    announcement = CodalAnnouncement.objects.get(pk=announcement_id)
    report, _ = CodalReport.objects.get_or_create(announcement=announcement)

    from django.conf import settings

    initial = classify_announcement(announcement)
    for field, value in initial.items():
        setattr(report, field, value)
    report.parser_version = settings.CODAL_PARSER_VERSION
    report.status = CodalReport.Status.FETCHING
    report.save()

    downloaded = []
    for kind, source_url in _artifact_sources(announcement):
        if not source_url:
            continue
        artifact, _ = CodalArtifact.objects.get_or_create(
            report=report, kind=kind, source_url=_absolute_url(source_url)
        )
        try:
            reserve_request(ARCHIVE)
            _final_url, content_type, content = download_artifact(source_url, kind)
            key, checksum = store_artifact(content, content_type, kind)
            artifact.s3_key = key
            artifact.checksum_sha256 = checksum
            artifact.content_type = content_type
            artifact.size_bytes = len(content)
            artifact.fetch_status = CodalArtifact.FetchStatus.STORED
            artifact.error_code = ""
            artifact.save()
            downloaded.append((kind, artifact, content))
        except QuotaExhausted:
            # Not this artifact's failure -- the day's provider budget is
            # spent. Stop the whole report rather than burning the same
            # exhausted budget on the next artifact/announcement in the
            # batch; the report stays at FETCHING and is picked up again
            # once it goes stale (CODAL_FETCHING_STALE_SECONDS).
            raise
        except CodalArtifactRejected as exc:
            artifact.fetch_status = CodalArtifact.FetchStatus.REJECTED
            artifact.error_code = str(exc)[:64]
            artifact.save(update_fields=["fetch_status", "error_code"])
        except CodalBlockedNetwork as exc:
            artifact.fetch_status = CodalArtifact.FetchStatus.BLOCKED_NETWORK
            artifact.error_code = str(exc)[:64]
            artifact.save(update_fields=["fetch_status", "error_code"])
        except CodalBlockedStorage as exc:
            artifact.fetch_status = CodalArtifact.FetchStatus.BLOCKED_STORAGE
            artifact.error_code = str(exc)[:64]
            artifact.save(update_fields=["fetch_status", "error_code"])
        except Exception as exc:
            # Anything unforeseen (a malformed header, a zlib error) used to
            # escape the whole loop and leave this row claiming "pending" --
            # the database said the artifact was never attempted when it had
            # been.
            artifact.fetch_status = CodalArtifact.FetchStatus.FAILED
            artifact.error_code = type(exc).__name__[:64]
            artifact.save(update_fields=["fetch_status", "error_code"])

    if not downloaded:
        statuses = set(report.artifacts.values_list("fetch_status", flat=True))
        if CodalArtifact.FetchStatus.BLOCKED_STORAGE in statuses:
            report.status = CodalReport.Status.BLOCKED_STORAGE
            report.error_code = "no_usable_artifact"
        elif CodalArtifact.FetchStatus.BLOCKED_NETWORK in statuses:
            report.status = CodalReport.Status.BLOCKED_NETWORK
            report.error_code = "no_usable_artifact"
        elif not statuses:
            # No artifact links at all -- nothing a retry could fetch.
            report.status = CodalReport.Status.UNSUPPORTED
            report.error_code = "no_artifact_links"
        elif statuses <= {CodalArtifact.FetchStatus.REJECTED}:
            # Every artifact failed its own content check (bad magic bytes,
            # disallowed host, oversized, zip bomb). That is a property of
            # the document, not the network -- retrying will not change it.
            report.status = CodalReport.Status.UNSUPPORTED
            report.error_code = "all_artifacts_rejected"
        else:
            report.status = CodalReport.Status.FAILED
            report.error_code = "no_usable_artifact"
        report.save(update_fields=["status", "error_code", "updated_at"])
        return report, {
            "error_code": report.error_code,
            "artifacts": _artifact_states(report),
        }

    parsed = None
    chosen = None
    parse_errors = []
    for kind, artifact, content in downloaded:
        if kind == CodalArtifact.Kind.ATTACHMENT:
            continue
        try:
            candidate = parse_artifact(kind, content)
        except Exception as exc:
            parse_errors.append(type(exc).__name__)
            continue
        if candidate.tables or candidate.sections or candidate.text:
            parsed, chosen = candidate, artifact
            break
    if parsed is None:
        # Downloaded fine, but nothing usable came out of any of them --
        # a template problem, not a network one. Not retried.
        report.status = CodalReport.Status.UNSUPPORTED
        report.error_code = "unparseable_template"
        report.save(update_fields=["status", "error_code", "updated_at"])
        return report, {"error_code": report.error_code, "parse_errors": parse_errors}

    metadata = classify_announcement(announcement, parsed.text)
    for field, value in metadata.items():
        setattr(report, field, value)
    extract_typed_facts(parsed, report.category, report.period_end)
    reconciled = category_reconciles(parsed, report.category)
    _persist_parsed(report, chosen, parsed)
    report.revision_of = _find_revision(report)
    if not report.category:
        report.status = CodalReport.Status.UNSUPPORTED
        report.quality = CodalReport.Quality.REVIEW
        report.error_code = "unknown_category"
    elif not reconciled:
        report.status = CodalReport.Status.NEEDS_REVIEW
        report.quality = CodalReport.Quality.REVIEW
        report.error_code = f"no_typed_facts:category={report.category}"
    else:
        report.status = CodalReport.Status.PARSED
        report.quality = CodalReport.Quality.VALIDATED
        report.error_code = ""
    report.extracted_at = timezone.now()
    report.save()
    announcement.category = report.category
    announcement.category_title = report.report_type
    announcement.is_audited = report.is_audited
    announcement.save(update_fields=["category", "category_title", "is_audited"])
    return report, {
        "artifact_count": len(downloaded),
        "parsed_from": chosen.kind if chosen else "",
        "artifacts": _artifact_states(report),
        "table_count": len(parsed.tables),
        "section_count": len(parsed.sections),
        "fact_count": len(parsed.facts),
    }
