from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0046_watchlist_item")]
    operations = [
        migrations.AddField(model_name="ledgerentry", name="removed_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="ledgerentry", name="revisions", field=models.JSONField(blank=True, default=list)),
    ]
