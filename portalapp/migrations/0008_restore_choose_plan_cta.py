from django.db import migrations


def restore_choose_plan_cta(apps, schema_editor):
    PricingPlan = apps.get_model("portalapp", "PricingPlan")
    PricingPlan.objects.filter(cta_label__in=["", "Contact us"]).update(cta_label="Choose plan")


class Migration(migrations.Migration):

    dependencies = [
        ("portalapp", "0007_contact_us_cta"),
    ]

    operations = [
        migrations.RunPython(restore_choose_plan_cta, migrations.RunPython.noop),
    ]
