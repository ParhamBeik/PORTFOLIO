"""Secure Codal artifact download and content-addressed S3/MinIO storage.

Ported from the pipeline stripped in commit 2ea22be, with local-disk storage
swapped for S3/MinIO (`CodalArtifact.s3_key` was already named for it). The
download-side SSRF/size/content protections are unchanged -- they have no
storage dependency.
"""
import functools
import hashlib
import io
import mimetypes
import zipfile
from urllib.parse import urljoin, urlsplit

import requests
from django.conf import settings

ALLOWED_HOSTS = frozenset({"codal.ir", "www.codal.ir", "excel.codal.ir"})
ALLOWED_TYPES = {
    "html": ("text/html", "application/xhtml+xml"),
    "excel": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/ms-excel",
        "application/x-msexcel",
        "application/octet-stream",
    ),
    "pdf": ("application/pdf", "application/octet-stream"),
    "attachment": (
        "application/pdf",
        "application/zip",
        "application/x-zip-compressed",
        "application/octet-stream",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/ms-excel",
        "application/x-msexcel",
    ),
}


class CodalBlockedNetwork(RuntimeError):
    pass


class CodalBlockedStorage(RuntimeError):
    pass


class CodalArtifactRejected(RuntimeError):
    pass


# ------------------------------------------------------------ reachability gate
#
# codal.ir is not reachable from every host this runs on. From the retired
# Frankfurt VPS, TCP 443 simply timed out, which put 4,728 artifacts in
# `blocked_network` and cost ~986 pointless connect attempts a day. The Iranian
# production VPS reaches it directly, but an outage is still a property of the
# network path, not of any one document, so retrying per-document learns nothing.
#
# One shared breaker: consecutive failures trip a cooldown, and exactly one probe
# is allowed through per cooldown to notice when the path comes back (e.g. when
# CODAL_HTTP_PROXY is finally pointed somewhere that can reach it).
_BREAKER_FAILS_KEY = "codal:origin:consecutive_failures"
_BREAKER_COOLDOWN_KEY = "codal:origin:cooldown"
_BREAKER_PROBE_PENDING_KEY = "codal:origin:probe_pending"
_BREAKER_PROBE_LOCK_KEY = "codal:origin:probe_lock"


def _breaker_client():
    from portfolio.live.redis_client import get_redis

    return get_redis()


def origin_unreachable(*, probe=False):
    """Whether the codal.ir path is currently considered down."""
    client = _breaker_client()
    if client is None:
        return False
    try:
        if client.get(_BREAKER_COOLDOWN_KEY):
            return True
        if not probe or not client.get(_BREAKER_PROBE_PENDING_KEY):
            return False
        return not bool(
            client.set(_BREAKER_PROBE_LOCK_KEY, "1", ex=300, nx=True)
        )
    except Exception:
        # A broken breaker must fail open. Refusing all downloads because Redis
        # blinked would be a worse outage than the one it guards against.
        return False


def _record_origin_failure():
    client = _breaker_client()
    if client is None:
        return
    try:
        fails = client.incr(_BREAKER_FAILS_KEY)
        client.expire(_BREAKER_FAILS_KEY, settings.CODAL_ORIGIN_COOLDOWN_SECONDS * 2)
        if fails >= settings.CODAL_ORIGIN_FAILURE_THRESHOLD:
            _park_origin(client)
    except Exception:
        pass


def _park_origin(client=None):
    """Open the breaker now: one cooldown, then a single recovery probe."""
    client = client or _breaker_client()
    if client is None:
        return
    try:
        client.set(_BREAKER_COOLDOWN_KEY, "1", ex=settings.CODAL_ORIGIN_COOLDOWN_SECONDS)
        client.set(
            _BREAKER_PROBE_PENDING_KEY, "1", ex=settings.CODAL_ORIGIN_COOLDOWN_SECONDS + 300
        )
        client.delete(_BREAKER_PROBE_LOCK_KEY)
        client.delete(_BREAKER_FAILS_KEY)
    except Exception:
        pass


# codal.ir answers a client it considers too busy with a CAPTCHA page ("تأیید
# کاربر": too many reports viewed, enter the security code) -- HTTP 200,
# text/html, ~5.5-6 KB, varying per request. Measured 2026-10-05 on Decision.aspx
# and Attachment.aspx. An `html` artifact accepts text/html, so without this the
# challenge would be stored as the filing itself. Both markers, so a filing that
# merely mentions one phrase is not mistaken for it.
_CHALLENGE_MARKERS = ("تأیید کاربر".encode(), "کد امنیتی".encode())


def _is_challenge(content):
    head = content[:20000]
    return all(marker in head for marker in _CHALLENGE_MARKERS)


def _record_origin_success():
    client = _breaker_client()
    if client is None:
        return
    try:
        client.delete(
            _BREAKER_FAILS_KEY,
            _BREAKER_COOLDOWN_KEY,
            _BREAKER_PROBE_PENDING_KEY,
            _BREAKER_PROBE_LOCK_KEY,
        )
    except Exception:
        pass


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
        if content.startswith(b"PK\x03\x04") or content.startswith(b"\xd0\xcf\x11\xe0"):
            return True
        sample = content[:2048].lstrip().lower()
        return b"<html" in sample or b"<!doctype html" in sample or b"<table" in sample
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
    """Fetch one Codal artifact, refusing anything that isn't what it claims to be.

    Host-allowlisted, redirect chains re-validated at every hop, streamed under
    a size cap, and checked against its own declared content-type and magic
    bytes -- codal.ir is an external, untrusted origin even though the URL
    itself came from a provider payload we otherwise trust.
    """
    proxy = settings.CODAL_HTTP_PROXY
    proxies = {"http": proxy, "https": proxy} if proxy else None
    session = requests.Session()
    current = _absolute_url(url)
    headers = {"User-Agent": "Portfolio-Codal-Warehouse/1.0"}
    origin_contacted = False
    try:
        if origin_unreachable(probe=True):
            raise CodalBlockedNetwork("origin_probe_in_flight")
        for _redirect in range(6):
            _validate_url(current)
            from .workflows import record_http_attempt

            record_http_attempt()
            response = session.get(
                current,
                headers=headers,
                proxies=proxies,
                timeout=(10, 30),
                stream=True,
                allow_redirects=False,
            )
            origin_contacted = True
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
            try:
                declared = int(response.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                declared = 0
            if declared > settings.CODAL_MAX_ARTIFACT_BYTES:
                raise CodalArtifactRejected("artifact_too_large")
            chunks, size = [], 0
            for chunk in response.iter_content(1024 * 1024):
                size += len(chunk)
                if size > settings.CODAL_MAX_ARTIFACT_BYTES:
                    raise CodalArtifactRejected("artifact_too_large")
                chunks.append(chunk)
            content = b"".join(chunks)
            if _is_challenge(content):
                # Not this document's fault and not a dead path: the origin is
                # rate-limiting us. Back off as a whole rather than per document,
                # and leave the report retryable.
                _park_origin()
                raise CodalBlockedNetwork(f"captcha_challenge@{urlsplit(current).hostname or '?'}")
            if not _valid_magic(kind, content):
                raise CodalArtifactRejected("invalid_content_signature")
            _check_archive(content)
            _record_origin_success()
            return current, content_type, content
        raise CodalArtifactRejected("too_many_redirects")
    except CodalArtifactRejected:
        # The origin answered; this document is just unusable. Not a network fault,
        # so it must not count toward the breaker.
        if origin_contacted:
            _record_origin_success()
        raise
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        label = f"HTTP{status}" if status else type(exc).__name__
        if status is None:
            # No response at all: connect timeout, DNS, refused. That is the path
            # being down rather than this URL being bad.
            _record_origin_failure()
        raise CodalBlockedNetwork(f"{label}@{urlsplit(current).hostname or '?'}") from exc
    finally:
        session.close()


@functools.lru_cache(maxsize=1)
def _client():
    """One pooled boto3 S3 client for the worker process, MinIO-compatible."""
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=settings.CODAL_S3_ENDPOINT_URL,
        aws_access_key_id=settings.CODAL_S3_ACCESS_KEY,
        aws_secret_access_key=settings.CODAL_S3_SECRET_KEY,
        region_name=settings.CODAL_S3_REGION,
    )


def _ensure_bucket():
    from botocore.exceptions import ClientError

    client = _client()
    try:
        client.head_bucket(Bucket=settings.CODAL_S3_BUCKET)
    except ClientError:
        client.create_bucket(Bucket=settings.CODAL_S3_BUCKET)


def store_artifact(content, content_type, kind):
    """Write one immutable, content-addressed artifact to S3/MinIO.

    The key is a pure function of the bytes (`codal/sha256/aa/<hash>.<ext>`),
    so re-storing the same document is a cheap no-op `put_object` rather than a
    duplicate. `head_object` before `put_object` skips a redundant upload of a
    document already stored under this checksum.
    """
    checksum = hashlib.sha256(content).hexdigest()
    extension = {"excel": "xlsx", "html": "html", "pdf": "pdf"}.get(kind)
    extension = extension or (mimetypes.guess_extension(content_type) or ".bin").lstrip(".")
    key = f"codal/sha256/{checksum[:2]}/{checksum}.{extension}"
    try:
        client = _client()
        try:
            client.head_object(Bucket=settings.CODAL_S3_BUCKET, Key=key)
        except Exception:
            _ensure_bucket()
            client.put_object(
                Bucket=settings.CODAL_S3_BUCKET,
                Key=key,
                Body=content,
                ContentType=content_type or "application/octet-stream",
            )
    except CodalBlockedStorage:
        raise
    except Exception as exc:
        raise CodalBlockedStorage(type(exc).__name__) from exc
    return key, checksum


def load_artifact(artifact):
    """Read archived bytes with the same size and digest checks used at ingest."""
    if not artifact.s3_key or not artifact.checksum_sha256:
        raise CodalBlockedStorage("missing_archive_reference")
    try:
        response = _client().get_object(
            Bucket=settings.CODAL_S3_BUCKET, Key=artifact.s3_key
        )
        body = response["Body"]
        try:
            content = body.read(settings.CODAL_MAX_ARTIFACT_BYTES + 1)
        finally:
            body.close()
    except Exception as exc:
        raise CodalBlockedStorage(type(exc).__name__) from exc
    if (
        len(content) > settings.CODAL_MAX_ARTIFACT_BYTES
        or len(content) != artifact.size_bytes
        or hashlib.sha256(content).hexdigest() != artifact.checksum_sha256
    ):
        raise CodalBlockedStorage("archive_integrity_mismatch")
    return content


def artifact_download_url(artifact, expires_in=3600):
    """A short-lived presigned URL for one stored artifact, or None if unstored."""
    from .models import CodalArtifact

    if artifact.fetch_status != CodalArtifact.FetchStatus.STORED or not artifact.s3_key:
        return None
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.CODAL_S3_BUCKET, "Key": artifact.s3_key},
        ExpiresIn=expires_in,
    )
