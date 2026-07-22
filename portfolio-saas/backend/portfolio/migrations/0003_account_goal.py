# Generated for the per-portfolio goal tag.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('portfolio', '0002_transaction'),
    ]

    operations = [
        migrations.AddField(
            model_name='account',
            name='goal',
            field=models.CharField(blank=True, default='', max_length=40),
        ),
    ]
