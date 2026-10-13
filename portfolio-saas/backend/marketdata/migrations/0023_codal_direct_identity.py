"""codal.ir becomes the source of the filing list (docs/CODAL-DIRECT-MIGRATION.md §5.3).

* CodalAnnouncement gains the identity codal.ir speaks (`tracing_no`,
  `letter_serial`, `letter_type`) and what it says exists (`has_*`), plus
  `source`. All nullable or defaulted: no table rewrite.
* CodalLetter.symbol widens to 255. 35 market-wide letters list several symbols
  in one field; at 64 each failed its page's write and the day never finished.
* Existing rows get `letter_serial` and `letter_type` from their own `link`
  (falling back to `link_pdf`'s `hs=`), so a crawled letter matches the row
  BrsApi already wrote instead of duplicating it. Decoded with `unquote`, never
  `parse_qsl`: 2,115 serials carry a literal `+`.
"""
import re
from urllib.parse import unquote

from django.db import migrations, models


def _query_value(url, name):
    match = re.search(rf"[?&]{name}=([^&]+)", url or "", re.I)
    return unquote(match.group(1)) if match else ""


def backfill(apps, schema_editor):
    Announcement = apps.get_model("marketdata", "CodalAnnouncement")
    batch = []
    rows = Announcement.objects.filter(letter_serial="").only("id", "link", "link_pdf")
    for row in rows.iterator(chunk_size=2000):
        serial = _query_value(row.link, "LetterSerial") or _query_value(row.link_pdf, "hs")
        letter_type = _query_value(row.link, "let") or _query_value(row.link_pdf, "let")
        if not serial and not letter_type:
            continue
        row.letter_serial = serial[:128]
        row.letter_type = int(letter_type) if letter_type.isdigit() and int(letter_type) < 32768 else None
        batch.append(row)
        if len(batch) >= 2000:
            Announcement.objects.bulk_update(batch, ["letter_serial", "letter_type"])
            batch = []
    if batch:
        Announcement.objects.bulk_update(batch, ["letter_serial", "letter_type"])


class Migration(migrations.Migration):

    dependencies = [
        ("marketdata", "0022_rejection_counts_and_refetch"),
    ]

    operations = [
        migrations.AlterField(
            model_name="codalletter",
            name="symbol",
            field=models.CharField(blank=True, db_index=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="codalannouncement",
            name="source",
            field=models.CharField(
                choices=[("brsapi", "BrsApi"), ("codal", "codal.ir")], default="brsapi", max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="codalannouncement",
            name="tracing_no",
            field=models.BigIntegerField(blank=True, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="codalannouncement",
            name="letter_serial",
            field=models.CharField(blank=True, db_index=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="codalannouncement",
            name="letter_type",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="codalannouncement",
            name="has_excel",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="codalannouncement",
            name="has_pdf",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="codalannouncement",
            name="has_attachment",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
