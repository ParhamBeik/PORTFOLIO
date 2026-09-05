"""Say what `started_on` has always meant.

Help text only -- no column changes, no data touched. `installments_paid`
counts whole months ELAPSED since this date, so nothing is paid on the day
itself and the n-th payment lands n months later; that is origination, not the
first installment. The old text said "Date the first installment was due",
which is off by one payment for the whole life of a loan. The arithmetic is
left alone deliberately: it is the reading three tests already pin, and moving
it would silently restate every existing balance.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('portfolio', '0035_liability_derived'),
    ]

    operations = [
        migrations.AlterField(
            model_name='liability',
            name='started_on',
            field=models.DateField(blank=True, help_text='Date the loan was taken out.', null=True),
        ),
    ]
