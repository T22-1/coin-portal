from django.db import migrations


def backfill(apps, schema_editor):
    Batch = apps.get_model("portalapp", "IncomingInventoryBatch")
    Line = apps.get_model("portalapp", "IncomingInventoryLine")
    alias = schema_editor.connection.alias
    for line in Line.objects.using(alias).filter(imported_item__isnull=False).iterator():
        line.imported_at = line.created_at
        line.checked = True
        line.received_quantity = 1
        line.allocated_cost = line.cost_basis
        line.save(using=alias, update_fields=["imported_at", "checked", "received_quantity", "allocated_cost"])
    for batch in Batch.objects.using(alias).all().iterator():
        lines = Line.objects.using(alias).filter(batch_id=batch.pk)
        if lines.exists() and not lines.filter(imported_at__isnull=True).exists():
            batch.stage = "ARCHIVED"
            batch.archived_at = batch.created_at
            batch.save(using=alias, update_fields=["stage", "archived_at"])
        elif lines.filter(imported_at__isnull=False).exists():
            # Legacy imports had no shipping allocation; preserve their recorded costs.
            for line in lines.filter(imported_at__isnull=True):
                line.allocated_cost = line.cost_basis
                line.save(using=alias, update_fields=["allocated_cost"])


class Migration(migrations.Migration):
    dependencies = [("portalapp", "0024_incoming_purchase_workflow")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
