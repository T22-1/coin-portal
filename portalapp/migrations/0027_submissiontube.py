from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [("portalapp", "0026_durable_incoming_documents")]

    operations = [
        migrations.CreateModel(
            name="SubmissionTube",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("declared_value", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                (
                    "submission",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tube_lines",
                        to="portalapp.submission",
                    ),
                ),
                (
                    "tube",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="submission_lines",
                        to="portalapp.container",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="submissiontube",
            constraint=models.UniqueConstraint(
                fields=("submission", "tube"),
                name="unique_tube_per_submission",
            ),
        ),
    ]
