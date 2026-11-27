from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0004_user_email_confirmed_at")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="limit_multiplier",
            field=models.PositiveSmallIntegerField(default=1),
        ),
    ]
