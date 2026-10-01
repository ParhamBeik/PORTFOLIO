import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portfolio", "0045_corporate_action_dismissal"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WatchlistItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("symbol", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="watchlist", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-id"]},
        ),
        migrations.AddConstraint(
            model_name="watchlistitem",
            constraint=models.UniqueConstraint(fields=("user", "symbol"), name="uniq_watchlist_user_symbol"),
        ),
    ]
