"""Secure Codal download, immutable object storage, and deterministic extraction."""

import hashlib
import io
import mimetypes
import os
import tempfile
import zipfile
from urllib.parse import urljoin, urlsplit

import requests
from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .codal_classification import classify_announcement
from .codal_parsers import category_reconciles, extract_typed_facts, parse_artifact
from .models import (
    CodalArtifact,
    CodalFact,
    CodalParsedTable,
    CodalReport,
    CodalSection,
)

ALLOWED_HOSTS = frozenset({"codal.ir", "www.codal.ir", "excel.codal.ir"})
ALLOWED_TYPES = {
    "html": ("text/html", "application/xhtml+xml"),
    "excel": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/octet-stream",
    ),
    "pdf": ("application/pdf", "application/octet-stream"),
    "attachment": (
        "application/pdf",
        "application/zip",
        "application/octet-stream",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
}


class CodalBlockedNetwork(RuntimeError):
    pass


class CodalBlockedStorage(RuntimeError):
    pass


class CodalArtifactRejected(RuntimeError):
    pass


def storage_error():
    """Why local storage is unusable, or None. Checked by writing, not guessing."""
    root = settings.CODAL_STORAGE_DIR
    if not root:
        return "CODAL_STORAGE_DIR"
    try:
        os.makedirs(root, exist_ok=True)
        handle, probe = tempfile.mkstemp(dir=root)
        os.close(handle)
        os.unlink(probe)
    except OSError as exc:
        return f"CODAL_STORAGE_DIR:{exc.__class__.__name__}"
    return None


def configuration_error():
    """Missing configuration, network requirement first.

    Storage used to need five S3 values; on local disk it needs one writable
    directory, so the proxy is the only thing a deployment must supply. Order
    matters: the caller maps a leading proxy entry to BLOCKED_NETWORK and a
    storage entry to BLOCKED_STORAGE.
    """
    missing = []
    if not settings.CODAL_HTTP_PROXY:
        missing.append("CODAL_HTTP_PROXY")
    storage = storage_error()
    if storage:
        missing.append(storage)
    return missing


def _absolute_url(value):
    return urljoin("https://codal.ir/", value or "")


def _validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise CodalArtifactRejected("disallowed_url")


def _valid_magic(kind, content):
    if kind == "pdf":
        return content.startswith(b"%PDF-")
    if kind == "excel":
        return content.startswith(b"PK\x03\x04") or content.startswith(b"\xd0\xcf\x11\xe0")
    if kind == "html":
        sample = content[:2048].lstrip().lower()
        return b"<html" in sample or b"<!doctype html" in sample or b"<table" in sample
    return bool(content)


def _check_archive(content):
    if not content.startswith(b"PK\x03\x04"):
        return
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            total = sum(item.file_size for item in archive.infolist())
            compressed = max(1, sum(item.compress_size for item in archive.infolist()))
            if total > settings.CODAL_MAX_ARTIFACT_BYTES * 5 or total / compressed > 100:
                raise CodalArtifactRejected("decompression_bomb")
    except zipfile.BadZipFile as exc:
        raise CodalArtifactRejected("invalid_zip") from exc


def download_artifact(url, kind):
    proxy = settings.CODAL_HTTP_PROXY
    session = requests.Session()
    current = _absolute_url(url)
    headers = {"User-Agent": "Portfolio-Codal-Warehouse/1.0"}
    try:
        for _redirect in range(6):
            _validate_url(current)
            from .workflows import record_http_attempt
            record_http_attempt()
            response = session.get(
                current,
                headers=headers,
                proxies={"http": proxy, "https": proxy},
                timeout=(10, 30),
                stream=True,
                allow_redirects=False,
            )
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise CodalArtifactRejected("redirect_without_location")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type not in ALLOWED_TYPES[kind]:
                raise CodalArtifactRejected("invalid_content_type")
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > settings.CODAL_MAX_ARTIFACT_BYTES:
                raise CodalArtifactRejected("artifact_too_large")
            chunks, size = [], 0
            for chunk in response.iter_content(1024 * 1024):
                size += len(chunk)
                if size > settings.CODAL_MAX_ARTIFACT_BYTES:
                    raise CodalArtifactRejected("artifact_too_large")
                chunks.append(chunk)
            content = b"".join(chunks)
            if not _valid_magic(kind, content):
                raise CodalArtifactRejected("invalid_content_signature")
            _check_archive(content)
            return current, content_type, content
        raise CodalArtifactRejected("too_many_redirects")
    except CodalArtifactRejected:
        raise
    except requests.RequestException as exc:
        raise CodalBlockedNetwork(type(exc).__name__) from exc
    finally:
        session.close()


def artifact_path(key):
    """Absolute path of a stored artifact, refusing to escape the storage root.

    `key` reaches this from the database, so it is treated as untrusted: a stored
    value of `../../etc/passwd` must not resolve outside the root.
    """
    root = os.path.realpath(settings.CODAL_STORAGE_DIR)
    path = os.path.realpath(os.path.join(root, key))
    if path != root and not path.startswith(root + os.sep):
        raise CodalBlockedStorage("path_escapes_storage_root")
    return path


def store_artifact(content, content_type, kind):
    """Write one immutable, content-addressed artifact to local disk.

    The layout is unchanged from the S3 version (`codal/sha256/aa/<hash>.<ext>`),
    so the key stays a pure function of the bytes and re-storing the same
    document is a no-op. Writing via a temp file and os.replace keeps that
    promise under a crash: a reader either sees the whole artifact or no file at
    all, never a truncated one sitting at a name that claims a checksum.
    """
    checksum = hashlib.sha256(content).hexdigest()
    extension = {"excel": "xlsx", "html": "html", "pdf": "pdf"}.get(kind)
    extension = extension or (mimetypes.guess_extension(content_type) or ".bin").lstrip(".")
    key = f"codal/sha256/{checksum[:2]}/{checksum}.{extension}"
    try:
        path = artifact_path(key)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=os.path.dirname(path))
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(content)
                os.replace(temporary, path)
            except BaseException:
                # A partial file under a checksum-named path would be a lie.
                if os.path.exists(temporary):
                    os.unlink(temporary)
                raise
    except CodalBlockedStorage:
        raise
    except Exception as exc:
        raise CodalBlockedStorage(type(exc).__name__) from exc
    return key, checksum


def artifact_download_url(artifact):
    """URL of the authenticated view that serves this artifact.

    There is no signing service on a local disk, so authorization is enforced by
    the view itself rather than by a short-lived URL. The storage directory is
    never served statically.
    """
    if artifact.fetch_status != CodalArtifact.FetchStatus.STORED or not artifact.s3_key:
        return None
    return reverse("codal-artifact-download", args=[artifact.pk])


def _artifact_sources(announcement):
    return [
        (CodalArtifact.Kind.EXCEL, announcement.link_excel),
        (CodalArtifact.Kind.HTML, announcement.link),
        (CodalArtifact.Kind.PDF, announcement.link_pdf),
        (CodalArtifact.Kind.ATTACHMENT, announcement.link_attachment),
    ]


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


def extract_report(report):
    missing = configuration_error()
    if missing:
        # Network is the harder blocker and is reported first when both apply:
        # a proxy needs a human, an unwritable directory needs a restart.
        report.status = (
            CodalReport.Status.BLOCKED_NETWORK
            if "CODAL_HTTP_PROXY" in missing
            else CodalReport.Status.BLOCKED_STORAGE
        )
        report.error_code = "missing_configuration"
        report.save(update_fields=["status", "error_code", "updated_at"])
        return report, {"error_code": "missing_configuration", "missing": missing}

    announcement = report.announcement
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

    if not downloaded:
        statuses = set(report.artifacts.values_list("fetch_status", flat=True))
        report.status = (
            CodalReport.Status.BLOCKED_STORAGE
            if CodalArtifact.FetchStatus.BLOCKED_STORAGE in statuses
            else CodalReport.Status.BLOCKED_NETWORK
            if CodalArtifact.FetchStatus.BLOCKED_NETWORK in statuses
            else CodalReport.Status.FAILED
        )
        report.error_code = "no_usable_artifact"
        report.save(update_fields=["status", "error_code", "updated_at"])
        return report, {"error_code": report.error_code}

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
        report.status = CodalReport.Status.UNSUPPORTED
        report.error_code = "unparseable_template"
        report.save(update_fields=["status", "error_code", "updated_at"])
        return report, {"error_code": report.error_code, "parse_errors": parse_errors}

    metadata = classify_announcement(announcement, parsed.text)
    for field, value in metadata.items():
        setattr(report, field, value)
    extract_typed_facts(parsed, report.category, report.period_end)
    reconciled = category_reconciles(parsed, report.category)
    ocr_publishable = not parsed.used_ocr or (
        parsed.confidence >= settings.CODAL_OCR_CONFIDENCE_THRESHOLD and reconciled
    )
    if not ocr_publishable:
        parsed.facts = []
    _persist_parsed(report, chosen, parsed)
    report.revision_of = _find_revision(report)
    if not report.category:
        report.status = CodalReport.Status.UNSUPPORTED
        report.quality = CodalReport.Quality.REVIEW
    elif parsed.used_ocr and not ocr_publishable:
        report.status = CodalReport.Status.NEEDS_REVIEW
        report.quality = CodalReport.Quality.REVIEW
    elif not reconciled:
        report.status = CodalReport.Status.NEEDS_REVIEW
        report.quality = CodalReport.Quality.REVIEW
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
        "table_count": len(parsed.tables),
        "section_count": len(parsed.sections),
        "fact_count": len(parsed.facts),
        "used_ocr": parsed.used_ocr,
        "confidence": parsed.confidence,
    }
