"""Deliberately NOT folded into 0001_squashed, unlike its 28 predecessors.

The squash can only be marked as already-applied when *every* migration in its
`replaces` list is recorded on the target database. Production had 0001-0028 and
never received this one, so including it here made the squash partially applied:
Django then discards the squash and falls back to the individual migrations,
which no longer exist on disk, and the deploy dies before the app starts.

Leaving this outside the squash is correct on both shapes of database. An
existing one has all 28 replaced migrations, marks the squash applied, and then
applies this single AddField. A fresh one runs the squash (which no longer
creates the column) and then this. Fold it in only once every deployed database
has it recorded.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portfolio", "0001_squashed"),
    ]

    operations = [
        migrations.AddField(
            model_name="snapshot",
            name="is_session_close",
            field=models.BooleanField(
                default=False,
                help_text="True when every held TSE asset used the verified close for its completed session.",
            ),
        ),
    ]
