from django.db import migrations


def update_cta_labels(apps, schema_editor):
    PricingPlan = apps.get_model("portalapp", "PricingPlan")
    PricingPlan.objects.filter(cta_label__in=["", "Choose plan"]).update(cta_label="Contact us")


class Migration(migrations.Migration):

    dependencies = [
        ("portalapp", "0006_contactlead"),
    ]

    operations = [
        migrations.RunPython(update_cta_labels, migrations.RunPython.noop),
    ]
