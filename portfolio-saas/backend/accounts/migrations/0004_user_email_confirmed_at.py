from django.db import migrations, models
from django.db.models import F


def confirm_existing(apps, schema_editor):
    # Every member who exists before confirmation was required keeps signing in.
    apps.get_model("accounts", "User").objects.update(email_confirmed_at=F("date_joined"))


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_user_role")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="email_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(confirm_existing, migrations.RunPython.noop),
    ]
