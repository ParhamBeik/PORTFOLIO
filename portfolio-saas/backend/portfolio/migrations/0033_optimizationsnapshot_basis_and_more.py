from django.conf import settings
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('portfolio', '0032_rights_issue_kind'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='optimizationsnapshot',
            name='basis',
            field=models.CharField(default='real_toman', max_length=32),
        ),
        migrations.AlterField(
            model_name='optimizationsnapshot',
            name='scenario',
            field=models.CharField(choices=[('max_sharpe', 'Max Sharpe'), ('min_volatility', 'Min Volatility'), ('equal_weight', 'Equal Weight'), ('risk_parity', 'Risk Parity'), ('hrp', 'HRP'), ('my_optimal', 'My Optimal')], default='max_sharpe', max_length=32),
        ),
        AddIndexConcurrently(
            model_name='optimizationsnapshot',
            index=models.Index(fields=['account', 'scenario', 'basis', '-created_at'], name='opt_snap_lookup_idx'),
        ),
    ]
