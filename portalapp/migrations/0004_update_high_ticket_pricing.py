from django.db import migrations


def update_high_ticket_pricing(apps, schema_editor):
    PricingPlan = apps.get_model("portalapp", "PricingPlan")
    for slug in ("starter", "pro", "enterprise"):
        PricingPlan.objects.filter(slug=slug).delete()

    plans = [
        {
            "name": "Launch",
            "slug": "launch",
            "tagline": "For getting CoinPortal 365 live with a clean operational foundation.",
            "price": "25000.00",
            "billing_interval": "ONE_TIME",
            "display_order": 10,
            "is_featured": False,
            "feature_bullets": "Core inventory workflow\nLabel printing\nSubmission packet setup\nAdmin pricing configuration",
        },
        {
            "name": "Growth",
            "slug": "growth",
            "tagline": "For active dealers who need grading, sales, and intake workflows tightened.",
            "price": "30000.00",
            "billing_interval": "ONE_TIME",
            "display_order": 20,
            "is_featured": True,
            "feature_bullets": "Everything in Launch\nPCGS, NGC, CAC, and CACG form workflows\nSale batch workflow\nSubmission status controls",
        },
        {
            "name": "Scale",
            "slug": "scale",
            "tagline": "For teams ready for invoice intake, automation, and deeper integrations.",
            "price": "35000.00",
            "billing_interval": "ONE_TIME",
            "display_order": 30,
            "is_featured": False,
            "feature_bullets": "Everything in Growth\nInvoice intake planning\nTeam workflow support\nIntegration planning\nAdvanced reporting roadmap",
        },
    ]
    for plan in plans:
        PricingPlan.objects.update_or_create(
            slug=plan["slug"],
            defaults={
                **plan,
                "currency": "USD",
                "trial_days": 0,
                "cta_label": "Choose plan",
                "is_active": True,
                "is_public": True,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("portalapp", "0003_pricingplan"),
    ]

    operations = [
        migrations.RunPython(update_high_ticket_pricing, migrations.RunPython.noop),
    ]
