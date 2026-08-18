"""Unit tests (pure functions, no network/S3): SSRF/content validation is the
security boundary of marketdata/codal_storage.py, and store_artifact's key
must stay a pure function of the bytes for its dedupe promise to hold."""
import pytest

from marketdata import codal_storage


def test_disallowed_host_is_rejected_before_any_network_call():
    with pytest.raises(codal_storage.CodalArtifactRejected, match="disallowed_url"):
        codal_storage.download_artifact("https://evil.example.com/x.pdf", "pdf")


def test_http_scheme_is_rejected():
    with pytest.raises(codal_storage.CodalArtifactRejected, match="disallowed_url"):
        codal_storage.download_artifact("http://codal.ir/x.pdf", "pdf")


@pytest.mark.parametrize("kind,content,expected", [
    ("pdf", b"%PDF-1.4 ...", True),
    ("pdf", b"not a pdf", False),
    ("excel", b"PK\x03\x04rest", True),
    ("excel", b"nope", False),
    ("html", b"<!doctype html><body>x</body>", True),
    ("html", b"plain text", False),
])
def test_magic_byte_check(kind, content, expected):
    assert codal_storage._valid_magic(kind, content) is expected


def test_store_artifact_key_is_a_pure_function_of_content(monkeypatch):
    calls = {"head": 0, "put": 0}

    class _FakeClient:
        def head_object(self, **kwargs):
            calls["head"] += 1
            raise Exception("NoSuchKey")

        def put_object(self, **kwargs):
            calls["put"] += 1

    monkeypatch.setattr(codal_storage, "_client", lambda: _FakeClient())
    monkeypatch.setattr(codal_storage, "_ensure_bucket", lambda: None)

    key1, checksum1 = codal_storage.store_artifact(b"same bytes", "application/pdf", "pdf")
    key2, checksum2 = codal_storage.store_artifact(b"same bytes", "application/pdf", "pdf")

    assert key1 == key2
    assert checksum1 == checksum2
    assert key1.startswith("codal/sha256/")
    assert key1.endswith(".pdf")
    assert calls["put"] == 2  # fake head_object always "misses" -- key derivation is what's under test


def test_store_artifact_wraps_backend_errors_as_blocked_storage(monkeypatch):
    class _FailingClient:
        def head_object(self, **kwargs):
            raise Exception("miss")

    def _boom():
        raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(codal_storage, "_client", lambda: _FailingClient())
    monkeypatch.setattr(codal_storage, "_ensure_bucket", _boom)

    with pytest.raises(codal_storage.CodalBlockedStorage):
        codal_storage.store_artifact(b"x", "application/pdf", "pdf")
