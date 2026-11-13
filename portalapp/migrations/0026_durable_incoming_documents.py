from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("portalapp", "0025_backfill_incoming_workflow")]

    operations = [
        migrations.AddField(
            model_name="incominginventorybatch",
            name="source_blob",
            field=models.BinaryField(blank=True, default=b""),
        ),
        migrations.AddField(
            model_name="incominginventorybatch",
            name="source_content_type",
            field=models.CharField(blank=True, max_length=127),
        ),
        migrations.AddField(
            model_name="incominginventorybatch",
            name="source_name",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
