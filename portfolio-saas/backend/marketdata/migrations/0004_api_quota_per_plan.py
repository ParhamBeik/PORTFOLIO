"""One quota row per (day, provider plan), and `limit` becomes observed data.

BrsApi meters each API key separately -- `Tsetmc/*` against TSETMC_API_KEY,
`Market/*` against BRS_API_KEY -- so a single row per day could not represent
the account. In production it hid the fact that the BRS plan sat 79% unused
while the shared 9,800 counter refused its requests.

Existing rows become `plan='tsetmc'`: they are ~97% TSETMC traffic, and that
attribution is closer to the truth than splitting a number nobody recorded.

`limit` now means "the ceiling the provider itself reported", so the inherited
9,800 is cleared -- it was our own guess at a combined cap, and leaving it would
apply a fictional ceiling to the live reserve on the very first request.
"""
from django.db import migrations, models


def clear_inherited_limits(apps, schema_editor):
    apps.get_model("marketdata", "ApiRequestQuota").objects.update(limit=0)


class Migration(migrations.Migration):

    dependencies = [
        ('marketdata', '0003_livefetchstate'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='apirequestquota',
            options={'ordering': ['-day', 'plan']},
        ),
        migrations.AddField(
            model_name='apirequestquota',
            name='plan',
            field=models.CharField(default='tsetmc', max_length=16),
        ),
        migrations.AlterField(
            model_name='apirequestquota',
            name='day',
            field=models.DateField(),
        ),
        migrations.AlterField(
            model_name='apirequestquota',
            name='limit',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddConstraint(
            model_name='apirequestquota',
            constraint=models.UniqueConstraint(fields=('day', 'plan'), name='uniq_api_quota_day_plan'),
        ),
        migrations.RunPython(clear_inherited_limits, migrations.RunPython.noop),
    ]
