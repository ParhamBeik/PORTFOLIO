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
from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .codal_classification import classify_announcement
from .codal_parsers import (
    category_reconciles, extract_typed_facts, parse_artifact, reconcile_monthly_sales,
)
from .codal_storage import (
    CodalArtifactRejected,
    CodalBlockedNetwork,
    CodalBlockedStorage,
    _absolute_url,
    download_artifact,
    load_artifact,
    store_artifact,
)
from .models import (
    CodalArtifact,
    CodalCandidateFact,
    CodalExtraction,
    CodalReport,
    CodalVerification,
)


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
    if not report.is_correction or not report.period_end or not report.announcement.date_publish:
        return None
    published = report.announcement
    earlier = Q(announcement__date_publish__lt=published.date_publish)
    if published.time_publish:
        earlier |= Q(
            announcement__date_publish=published.date_publish,
            announcement__time_publish__lt=published.time_publish,
        )
    return (
        CodalReport.objects.filter(
            announcement__symbol=published.symbol,
            report_type=report.report_type,
            period_end=report.period_end,
            is_consolidated=report.is_consolidated,
        )
        .filter(earlier)
        .exclude(pk=report.pk)
        .order_by("-announcement__date_publish", "-announcement__time_publish", "-pk")
        .first()
    )


def _persist_parsed(report, artifact, parsed):
    # Old extractions remain evidence, including the legacy tables and facts.
    # A new parser version creates a new run; retrying the same exact bytes and
    # version reuses that run instead of replacing its facts.
    if not parsed.facts and (report.extractions.exists() or report.facts.exists()):
        raise CodalExtractionRegressed(
            "parse produced 0 facts despite an earlier extraction"
        )
    with transaction.atomic():
        extraction, created = CodalExtraction.objects.get_or_create(
            report=report, artifact=artifact,
            checksum_sha256=artifact.checksum_sha256,
            parser_version=report.parser_version,
            defaults={
                "table_count": len(parsed.tables),
                "section_count": len(parsed.sections),
                "fact_count": len(parsed.facts),
            },
        )
        if not created:
            return extraction
        _max_dec = Decimal("1e26")
        reconciled_total = reconcile_monthly_sales(parsed.facts) if report.category == 3 else None
        candidates = []
        for fact in parsed.facts:
            num_val = fact.get("numeric_value")
            if num_val is not None:
                try:
                    if abs(Decimal(str(num_val))) >= _max_dec:
                        num_val = None
                except (InvalidOperation, TypeError):
                    num_val = None
            candidates.append(CodalCandidateFact(
                extraction=extraction,
                fact_code=fact["fact_code"],
                raw_value=fact["raw_value"],
                numeric_value=num_val,
                unit=fact.get("unit", ""),
                currency=fact.get("currency", ""),
                period_start=fact.get("period_start", ""),
                period_end=fact.get("period_end", ""),
                dimensions=fact.get("dimensions", {}),
                source_coordinates=fact["source_coordinates"],
                verification_status=(
                    CodalVerification.RECONCILED if fact is reconciled_total
                    else CodalVerification.EXTRACTED
                ),
            ))
        CodalCandidateFact.objects.bulk_create(candidates, batch_size=1000)
        return extraction


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
            if artifact.fetch_status == CodalArtifact.FetchStatus.STORED and artifact.s3_key:
                content = load_artifact(artifact)
                downloaded.append((kind, artifact, content))
                continue
            # No quota reservation. These bytes come from codal.ir, not from the
            # metered provider -- charging them to the BrsApi archive budget spent
            # ~1,000 requests/day of somebody else's allowance and, on a day when
            # the budget ran out, stopped document extraction for a reason that
            # had nothing to do with documents.
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
    try:
        _persist_parsed(report, chosen, parsed)
    except CodalExtractionRegressed:
        report.status = CodalReport.Status.NEEDS_REVIEW
        report.quality = CodalReport.Quality.REVIEW
        report.verification_status = CodalVerification.QUARANTINED
        report.error_code = "extraction_regressed"
        report.save(update_fields=[
            "status", "quality", "verification_status", "error_code", "updated_at",
        ])
        return report, {"error_code": report.error_code, "artifacts": _artifact_states(report)}
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
        report.quality = CodalReport.Quality.DEGRADED
        report.error_code = ""
    report.verification_status = CodalVerification.EXTRACTED
    report.extracted_at = timezone.now()
    report.save()
    # Report category/audit flags are interpretations. Never write them over
    # the announcement's source fields: a subsequent classifier would treat an
    # inferred category as a provider-confirmed fact.
    return report, {
        "artifact_count": len(downloaded),
        "parsed_from": chosen.kind if chosen else "",
        "artifacts": _artifact_states(report),
        "table_count": len(parsed.tables),
        "section_count": len(parsed.sections),
        "fact_count": len(parsed.facts),
    }
