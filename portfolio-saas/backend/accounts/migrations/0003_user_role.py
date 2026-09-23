from django.db import migrations, models
from django.db.models import Q


def backfill_roles(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(Q(is_staff=True) | Q(is_superuser=True)).update(
        role="admin", is_staff=True, is_superuser=True
    )
    User.objects.filter(role="user").update(is_staff=False, is_superuser=False)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_lowercase_emails")]

    operations = [
        migrations.AlterModelOptions(name="user", options={}),
        migrations.AddField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[("admin", "Admin"), ("user", "User")],
                default="user",
                max_length=5,
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="risk_profile",
            field=models.CharField(
                choices=[("conservative", "Conservative"), ("balanced", "Balanced"), ("growth", "Growth")],
                default="balanced",
                max_length=12,
            ),
        ),
        migrations.RunPython(backfill_roles, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=(
                    Q(role="admin", is_staff=True, is_superuser=True)
                    | Q(role="user", is_staff=False, is_superuser=False)
                ),
                name="user_role_matches_django_flags",
            ),
        ),
    ]
