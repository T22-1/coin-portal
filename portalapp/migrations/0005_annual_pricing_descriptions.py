from django.db import migrations, models


def update_annual_pricing(apps, schema_editor):
    PricingPlan = apps.get_model("portalapp", "PricingPlan")
    plans = [
        {
            "name": "Launch",
            "slug": "launch",
            "tagline": "A structured implementation package for an established coin business.",
            "description": "Launch covers configuration, onboarding, and the initial operating setup for CoinPortal 365. It is designed for businesses that need reliable inventory controls, admin workflows, and documented processes before payment activation.",
            "price": "25000.00",
            "billing_interval": "YEAR",
            "display_order": 10,
            "is_featured": False,
            "feature_bullets": "12-month platform access\nCore inventory workflow\nLabel printing\nSubmission packet setup\nAdmin pricing configuration\nOperational onboarding support",
        },
        {
            "name": "Growth",
            "slug": "growth",
            "tagline": "Expanded operating support for active dealers with grading and sales volume.",
            "description": "Growth adds submission, sales, and workflow tooling for dealers processing inventory through multiple grading services. It is priced as an annual business software package with clear deliverables and ongoing operational support.",
            "price": "35000.00",
            "billing_interval": "YEAR",
            "display_order": 20,
            "is_featured": True,
            "feature_bullets": "12-month platform access\nEverything in Launch\nPCGS, NGC, CAC, and CACG form workflows\nSale batch workflow\nSubmission status controls\nWorkflow refinement support",
        },
        {
            "name": "Scale",
            "slug": "scale",
            "tagline": "Advanced annual package for teams needing automation and integration planning.",
            "description": "Scale is for organizations that require a deeper implementation, advanced workflow planning, and support for automation initiatives such as invoice intake, reporting, and future integrations. The plan is structured as a 12-month professional software and services engagement.",
            "price": "75000.00",
            "billing_interval": "YEAR",
            "display_order": 30,
            "is_featured": False,
            "feature_bullets": "12-month platform access\nEverything in Growth\nInvoice intake planning\nTeam workflow support\nIntegration planning\nAdvanced reporting roadmap\nPriority implementation support",
        },
    ]
    for plan in plans:
        PricingPlan.objects.update_or_create(
            slug=plan["slug"],
            defaults={
                **plan,
                "currency": "USD",
                "trial_days": 0,
                "cta_label": "Contact us",
                "is_active": True,
                "is_public": True,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("portalapp", "0004_update_high_ticket_pricing"),
    ]

    operations = [
        migrations.AlterField(
            model_name="pricingplan",
            name="billing_interval",
            field=models.CharField(
                choices=[
                    ("MONTH", "Monthly"),
                    ("YEAR", "12-month"),
                    ("ONE_TIME", "One-time"),
                    ("CUSTOM", "Custom"),
                ],
                default="MONTH",
                max_length=10,
            ),
        ),
        migrations.RunPython(update_annual_pricing, migrations.RunPython.noop),
    ]
