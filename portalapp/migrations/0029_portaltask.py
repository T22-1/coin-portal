from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("portalapp", "0028_listing_types"),
    ]
    operations = [
        migrations.CreateModel(
            name="PortalTask",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=180)),
                ("details", models.TextField(blank=True)),
                ("priority", models.CharField(choices=[("NORMAL", "Normal"), ("HIGH", "High"), ("URGENT", "Urgent")], default="NORMAL", max_length=12)),
                ("status", models.CharField(choices=[("OPEN", "Open"), ("IN_PROGRESS", "In progress"), ("DONE", "Done")], default="OPEN", max_length=16)),
                ("due_date", models.DateField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("assigned_to", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="assigned_portal_tasks", to=settings.AUTH_USER_MODEL)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="created_portal_tasks", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("status", "due_date", "-priority", "-created_at")},
        ),
    ]
