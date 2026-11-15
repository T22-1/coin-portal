from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("portalapp", "0027_submissiontube")]

    operations = [
        migrations.AddField(
            model_name="incominginventoryline",
            name="listing_type",
            field=models.CharField(
                choices=[("FIXED", "Fixed price"), ("AUCTION", "Auction")],
                default="FIXED",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="inventoryitem",
            name="listing_type",
            field=models.CharField(
                choices=[("FIXED", "Fixed price"), ("AUCTION", "Auction")],
                default="FIXED",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="container",
            name="listing_type",
            field=models.CharField(
                choices=[("FIXED", "Fixed price"), ("AUCTION", "Auction")],
                default="FIXED",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="listing_type",
            field=models.CharField(
                choices=[("FIXED", "Fixed price"), ("AUCTION", "Auction")],
                default="FIXED",
                max_length=12,
            ),
        ),
    ]
