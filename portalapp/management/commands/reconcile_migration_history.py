from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder


HISTORICAL_MIGRATIONS = (
    ("0002_add_submission_shipping_fields", {"portalapp_submission": {"carrier", "grading_submission_number", "show_name", "submission_method", "tracking_number"}}),
    ("0003_pricingplan", {"portalapp_pricingplan": set()}),
    ("0004_update_high_ticket_pricing", {"portalapp_pricingplan": set()}),
    ("0005_annual_pricing_descriptions", {"portalapp_pricingplan": set()}),
    ("0006_contactlead", {"portalapp_contactlead": set()}),
    ("0007_contact_us_cta", {"portalapp_contactlead": set()}),
    ("0008_restore_choose_plan_cta", {"portalapp_contactlead": set()}),
    ("0009_incominginventorybatch_incominginventoryline", {
        "portalapp_incominginventorybatch": {"source_file", "parser_status"},
        "portalapp_incominginventoryline": {"batch_id", "imported_item_id"},
    }),
    ("0010_alter_container_options", {"portalapp_container": {"internal_id"}}),
    ("0011_alter_inventoryitem_cac_sticker_and_more", {"portalapp_inventoryitem": {"cac_sticker", "date_mm", "grade_text", "holder", "internal_id"}}),
    ("0012_alter_inventoryitem_cert_number", {"portalapp_inventoryitem": {"cert_number"}}),
    ("0013_saletube", {"portalapp_saletube": {"sale_id", "tube_id"}}),
    ("0014_alter_incominginventorybatch_options", {"portalapp_incominginventorybatch": set()}),
    ("0015_alter_container_internal_id", {"portalapp_container": {"internal_id"}}),
    ("0016_container_coin_fields", {"portalapp_container": {"date_mm", "denomination", "series"}}),
    ("0017_alter_container_quantity", {"portalapp_container": {"quantity"}}),
    # Report is unmanaged, so its migration only updates Django's model state.
    ("0018_report", {"portalapp_container": set()}),
    ("0019_product", {"portalapp_product": set()}),
    ("0020_container_product_cost_basis", {
        "portalapp_container": {"cost_basis"},
        "portalapp_product": {"cost_basis"},
    }),
    ("0021_inventory_numismatic_proxy", {"portalapp_inventoryitem": set()}),
    ("0022_rawitem_proxy", {"portalapp_inventoryitem": set()}),
)


class Command(BaseCommand):
    help = "Reconcile legacy schema changes that predate the production migration ledger."

    def handle(self, *args, **options):
        recorder = MigrationRecorder(connection)
        applied = set(recorder.applied_migrations())
        if ("portalapp", "0001_initial") not in applied:
            raise CommandError("portalapp.0001_initial is not recorded; refusing to reconcile migration history.")

        tables = set(connection.introspection.table_names())
        column_cache = {}

        def columns(table):
            if table not in column_cache:
                with connection.cursor() as cursor:
                    column_cache[table] = {
                        field.name for field in connection.introspection.get_table_description(cursor, table)
                    }
            return column_cache[table]

        recorded = []
        applied_normally = []
        for migration, signatures in HISTORICAL_MIGRATIONS:
            key = ("portalapp", migration)
            if key in applied:
                continue
            schema_exists = True
            for table, expected_columns in signatures.items():
                if table not in tables:
                    schema_exists = False
                    break
                missing = expected_columns - columns(table)
                if missing:
                    schema_exists = False
                    break

            if schema_exists:
                recorder.record_applied("portalapp", migration)
                recorded.append(migration)
            else:
                call_command(
                    "migrate",
                    "portalapp",
                    migration,
                    interactive=False,
                    verbosity=options["verbosity"],
                    stdout=self.stdout,
                )
                tables = set(connection.introspection.table_names())
                column_cache.clear()
                applied_normally.append(migration)
            applied.add(key)

        if recorded:
            self.stdout.write(self.style.SUCCESS(
                "Recorded existing legacy schema migrations: " + ", ".join(recorded)
            ))
        if applied_normally:
            self.stdout.write(self.style.SUCCESS(
                "Applied missing legacy schema migrations: " + ", ".join(applied_normally)
            ))
        if not recorded and not applied_normally:
            self.stdout.write("Legacy migration history is already reconciled.")
