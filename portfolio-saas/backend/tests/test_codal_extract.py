import pytest

from marketdata import codal_extract
from marketdata.models import CodalAnnouncement, CodalReport


class _Response:
    content = b"artifact"

    def raise_for_status(self):
        return None


@pytest.mark.django_db
def test_extract_marks_report_blocked_when_mongo_write_fails(monkeypatch):
    announcement = CodalAnnouncement.objects.create(
        symbol="TEST",
        title="Test report",
        link="https://example.test/report",
    )
    monkeypatch.setattr(codal_extract.requests, "get", lambda *args, **kwargs: _Response())
    monkeypatch.setattr(codal_extract, "_text", lambda *args: "parsed text")

    def unavailable():
        raise RuntimeError("Mongo unavailable")

    monkeypatch.setattr(codal_extract, "_document_collection", unavailable)

    with pytest.raises(RuntimeError, match="Mongo unavailable"):
        codal_extract.extract(announcement.pk)

    assert CodalReport.objects.get(announcement=announcement).status == CodalReport.Status.BLOCKED_STORAGE
