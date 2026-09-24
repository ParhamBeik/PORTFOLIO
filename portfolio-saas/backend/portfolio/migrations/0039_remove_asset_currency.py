from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0038_current_optimization_snapshots")]

    operations = [migrations.RemoveField(model_name="asset", name="currency")]
