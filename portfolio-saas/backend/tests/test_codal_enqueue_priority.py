"""Monthly activity reports are processed before the rest of the corpus.

They are the one uniform, Excel-backed family big enough to prove the pipeline
on: 17,808 documents over 725 companies, no OCR. Spreading the first runs across
~50 templates would tell us much less.
"""
from unittest.mock import patch

import pytest

from marketdata.models import CodalAnnouncement, CodalReport
from marketdata.tasks import CODAL_PRIORITY_TITLE, enqueue_codal_reports

pytestmark = pytest.mark.django_db


def _announcement(title, day, code="ن-10"):
    return CodalAnnouncement.objects.create(
        symbol="فولاد", title=title, code=code,
        date_publish=day, time_publish="10:00:00",
        link=f"https://codal.ir/{day}",
    )


@pytest.fixture(autouse=True)
def _enabled(settings):
    settings.CODAL_EXTRACTION_ENABLED = True
    settings.CODAL_ENQUEUE_BATCH_SIZE = 2


def test_monthly_reports_go_first():
    _announcement("معرفی /تغییر در ترکیب اعضای هیئت مدیره", "1405-05-17")
    _announcement("مشخصات کمیته حسابرسی", "1405-05-16")
    monthly = _announcement(f"{CODAL_PRIORITY_TITLE} دوره ۱ ماهه", "1405-04-31")

    with patch("marketdata.tasks.process_codal_report.delay") as delay:
        enqueue_codal_reports()

    queued = {call.args[0] for call in delay.call_args_list}
    assert CodalReport.objects.get(announcement=monthly).pk in queued


def test_newest_first_within_the_priority_family():
    older = _announcement(f"{CODAL_PRIORITY_TITLE} الف", "1404-01-01")
    newer = _announcement(f"{CODAL_PRIORITY_TITLE} ب", "1405-05-17")
    newest = _announcement(f"{CODAL_PRIORITY_TITLE} ج", "1405-05-18")

    with patch("marketdata.tasks.process_codal_report.delay") as delay:
        enqueue_codal_reports()

    queued = {call.args[0] for call in delay.call_args_list}
    assert CodalReport.objects.get(announcement=newest).pk in queued
    assert CodalReport.objects.get(announcement=newer).pk in queued
    # Beyond the batch: no report row is created for it at all, so it is still
    # pending for the next tick rather than silently marked as attempted.
    assert not CodalReport.objects.filter(announcement=older).exists()


def test_the_rest_of_the_corpus_still_gets_processed():
    """Priority must not mean starvation once the family is exhausted."""
    other = _announcement("مشخصات کمیته ریسک", "1405-05-16")

    with patch("marketdata.tasks.process_codal_report.delay") as delay:
        enqueue_codal_reports()

    queued = {call.args[0] for call in delay.call_args_list}
    assert CodalReport.objects.get(announcement=other).pk in queued


def test_disabled_extraction_enqueues_nothing(settings):
    settings.CODAL_EXTRACTION_ENABLED = False
    _announcement(f"{CODAL_PRIORITY_TITLE} الف", "1405-05-17")

    with patch("marketdata.tasks.process_codal_report.delay") as delay:
        enqueue_codal_reports()

    assert delay.call_count == 0
