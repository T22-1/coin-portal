import os
import tempfile
import json
from io import BytesIO
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .intake import LineForm
from .intake_parser import read_purchase
from .models import IncomingInventoryBatch as Batch, IncomingInventoryLine as Line, InventoryItem, Container, Product


def purchase_post(batch, action="save", selected=()):
    data = {"title": batch.title, "vendor": batch.vendor, "invoice_number": batch.invoice_number,
            "shipping": str(batch.shipping), "tax": str(batch.tax), "action": action}
    pending = list(batch.lines.filter(imported_at__isnull=True, imported_item__isnull=True))
    data.update({"lines-TOTAL_FORMS": str(len(pending)), "lines-INITIAL_FORMS": str(len(pending))})
    for index, line in enumerate(pending):
        data[f"lines-{index}-id"] = str(line.pk)
        for field in LineForm.Meta.fields:
            value = getattr(line, field)
            if isinstance(value, bool):
                if value:
                    data[f"lines-{index}-{field}"] = "on"
            else:
                data[f"lines-{index}-{field}"] = "" if value is None else str(value)
        if line.pk in selected:
            data[f"lines-{index}-selected"] = "on"
    return data


class IntakeTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = override_settings(MEDIA_ROOT=self.temp.name)
        settings.enable()
        self.addCleanup(settings.disable)
        env = patch.dict(os.environ, {"OPENAI_API_KEY": ""})
        env.start()
        self.addCleanup(env.stop)
        self.user = User.objects.create_superuser("intake", "", "password")
        self.client.force_login(self.user)
        self.batch = Batch.objects.create(title="eBay rolls", stage="QC", shipping=Decimal("5"), tax=Decimal("1"))

    def line(self, **kwargs):
        values = dict(raw_description="1943-D 1c roll", date_mm="1943-D", denomination="1c", series="Lincoln Cent",
                      destination="TUBES", quantity=1, received_quantity=1, checked=True, cost_basis=Decimal("10"))
        values.update(kwargs)
        return self.batch.lines.create(**values)

    def post(self, action, selected=()):
        return self.client.post(reverse("incoming_inventory_batch", args=[self.batch.pk]), purchase_post(self.batch, action, selected))

    def test_categories_costs_partial_approval_and_archive(self):
        tube = self.line()
        raw = self.line(destination="RAW")
        numismatic = self.line(destination="NUMISMATIC", holder="PCGS", cert_number="12345")
        product = self.line(destination="PRODUCTS", quantity=3, received_quantity=3)
        response = self.post("import", [tube.pk, raw.pk])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Container.objects.get().cost_basis, Decimal("11.50"))
        self.assertEqual(InventoryItem.objects.get().holder, "RAW")
        self.assertEqual(self.post("import", [numismatic.pk, product.pk]).status_code, 302)
        total = sum(Container.objects.values_list("cost_basis", flat=True)) + sum(InventoryItem.objects.values_list("cost_basis", flat=True))
        total += sum(p.cost_basis * p.quantity for p in Product.objects.all())
        self.assertEqual(total, Decimal("46.00"))
        self.assertEqual(self.post("archive").status_code, 302)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.stage, "ARCHIVED")
        self.assertContains(self.client.get(reverse("incoming_inventory_batch", args=[self.batch.pk])), "Reopen Purchase")

    def test_repeat_and_stale_post_cannot_duplicate(self):
        line = self.line()
        url = reverse("incoming_inventory_batch", args=[self.batch.pk])
        data = purchase_post(self.batch, "import", [line.pk])
        self.client.post(url, data)
        self.client.post(url, data)
        self.assertEqual(Container.objects.count(), 1)
        self.assertEqual(self.batch.lines.count(), 1)

    def test_unreceived_or_unchecked_or_wrong_stage_blocked(self):
        line = self.line(received_quantity=0)
        self.assertContains(self.post("import", [line.pk]), "confirm inspection")
        line.received_quantity = 1
        line.checked = False
        line.save()
        self.post("import", [line.pk])
        line.checked = True
        line.save()
        self.batch.stage = "AWAITING"
        self.batch.save()
        self.assertContains(self.post("import", [line.pk]), "Quality Check after it arrives")
        self.assertFalse(Container.objects.exists())

    def test_atomic_approval_failure(self):
        one = self.line()
        two = self.line(destination="RAW", received_quantity=0)
        self.post("import", [one.pk, two.pk])
        self.assertFalse(Container.objects.exists())
        self.assertFalse(InventoryItem.objects.exists())

    def test_split_conserves_quantity_and_cost(self):
        line = self.line(quantity=3, cost_basis=Decimal("10"))
        self.assertEqual(self.post(f"split:{line.pk}").status_code, 302)
        self.assertEqual(sum(self.batch.lines.values_list("quantity", flat=True)), 3)
        self.assertEqual(sum(self.batch.lines.values_list("cost_basis", flat=True)), Decimal("10"))

    def test_archive_requires_resolved_rows(self):
        self.line()
        self.assertContains(self.post("archive"), "Approve or exclude")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.stage, "QC")

    def test_invalid_cost_and_cross_batch_ids_rejected(self):
        line = self.line()
        url = reverse("incoming_inventory_batch", args=[self.batch.pk])
        data = purchase_post(self.batch, "import", [line.pk])
        data["lines-0-cost_basis"] = "-1"
        self.assertContains(self.client.post(url, data), "positive amount")
        other = Batch.objects.create()
        foreign = other.lines.create(quantity=1)
        data = purchase_post(self.batch, "import", [line.pk])
        data["lines-0-id"] = str(foreign.pk)
        self.client.post(url, data)
        self.assertFalse(Container.objects.exists())

    def test_file_duplicate_and_automatic_import_disabled(self):
        content = b"description,date,denom,cost\n1943-D cent roll,1943-D,1c,20\n"
        url = reverse("incoming_inventory_upload")
        first = self.client.post(url, {"invoice": SimpleUploadedFile("a.csv", content), "auto_import_ready": "on"})
        second = self.client.post(url, {"invoice": SimpleUploadedFile("b.csv", content)})
        self.assertEqual(first.url, second.url)
        self.assertEqual(Batch.objects.exclude(pk=self.batch.pk).count(), 1)
        self.assertFalse(Container.objects.exists())
        self.assertFalse(InventoryItem.objects.exists())

    def test_permission_required(self):
        user = User.objects.create_user("reader", password="password")
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("incoming_inventory")).status_code, 403)
        self.assertEqual(self.post("add").status_code, 403)

    def test_filters_and_manual_row(self):
        self.assertEqual(self.post("add").status_code, 302)
        self.assertEqual(self.batch.lines.count(), 1)
        self.assertContains(self.client.get(reverse("incoming_inventory"), {"stage": "QC"}), "eBay rolls")
        self.assertNotContains(self.client.get(reverse("incoming_inventory"), {"stage": "ARCHIVED"}), "eBay rolls")

    def test_mixed_mint_rolls_local(self):
        _, rows, _, _ = read_purchase(SimpleUploadedFile("order.txt", b"3 rolls 1943 P,D,S Lincoln cent $90.00"))
        self.assertEqual([row["date_mm"] for row in rows], ["1943-P", "1943-D", "1943-S"])
        self.assertEqual(sum(row["cost_basis"] for row in rows), Decimal("90"))
        self.assertTrue(all(row["destination"] == "TUBES" and row["quantity"] == 1 for row in rows))
        self.assertTrue(all(row["coins_per_roll"] is None for row in rows))

    def test_scanned_document_without_ai_keeps_manual_path(self):
        response = self.client.post(reverse("incoming_inventory_upload"), {"invoice": SimpleUploadedFile("order.png", b"image")})
        self.assertEqual(response.status_code, 302)
        batch = Batch.objects.exclude(pk=self.batch.pk).get()
        self.assertEqual(batch.stage, "REVIEW")
        self.assertEqual(batch.lines.count(), 0)

    def test_creation_failure_rolls_back_all_inventory(self):
        line = self.line(quantity=2, received_quantity=2)
        from django.db import IntegrityError
        with patch("portalapp.intake.Container.objects.create", side_effect=IntegrityError("collision")):
            self.post("import", [line.pk])
        line.refresh_from_db()
        self.assertIsNone(line.imported_at)
        self.assertFalse(Container.objects.exists())

    def test_excel_and_pdf_readers(self):
        from openpyxl import Workbook
        from reportlab.pdfgen.canvas import Canvas
        book = Workbook()
        book.active.append(["description", "date", "denom", "quantity", "line total"])
        book.active.append(["1943-D cent roll", "1943-D", "1c", 3, 90])
        out = BytesIO()
        book.save(out)
        _, rows, _, _ = read_purchase(SimpleUploadedFile("order.xlsx", out.getvalue()))
        self.assertEqual(rows[0]["quantity"], 3)
        self.assertEqual(rows[0]["cost_basis"], Decimal("90"))
        out = BytesIO()
        pdf = Canvas(out)
        pdf.drawString(40, 700, "1943-D 1c Lincoln Cent roll $30.00")
        pdf.save()
        _, rows, _, _ = read_purchase(SimpleUploadedFile("order.pdf", out.getvalue()))
        self.assertEqual(rows[0]["destination"], "TUBES")

    def test_ai_image_metadata_and_duplicate_order(self):
        payload = {"vendor": "Example seller", "invoice_number": "ORDER-10", "shipping": "5.00", "tax": "2.00",
                   "purchase_date": "2026-11-07", "items": [{"raw_description": "1943-D roll", "quantity": 1,
                   "destination": "TUBES", "date_mm": "1943-D", "denomination": "1c", "cost_basis": "30"}]}
        def response(*args, **kwargs):
            return BytesIO(json.dumps({"output_text": json.dumps(payload)}).encode())
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), patch("urllib.request.urlopen", side_effect=response) as api:
            first = self.client.post(reverse("incoming_inventory_upload"), {"invoice": SimpleUploadedFile("a.png", b"first")})
            second = self.client.post(reverse("incoming_inventory_upload"), {"invoice": SimpleUploadedFile("b.png", b"second")})
            self.assertEqual(first.url, second.url)
            self.assertIn(b"input_image", api.call_args.args[0].data)
        batch = Batch.objects.exclude(pk=self.batch.pk).get()
        self.assertEqual(batch.shipping, Decimal("5"))
        self.assertEqual(batch.lines.count(), 1)

    def test_missing_cost_blocks_approval_and_document_requires_permission(self):
        line = self.line(cost_basis=None)
        self.assertContains(self.post("import", [line.pk]), "Enter merchandise costs")
        self.client.logout()
        response = self.client.get(reverse("incoming_document", args=[self.batch.pk]))
        self.assertEqual(response.status_code, 302)

    def test_ai_timeout_falls_back_to_local_reader(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), patch("urllib.request.urlopen", side_effect=TimeoutError):
            response = self.client.post(reverse("incoming_inventory_upload"), {
                "invoice": SimpleUploadedFile("timeout.txt", b"1943-D 1c Lincoln Cent roll $30.00")})
        self.assertEqual(response.status_code, 302)
        batch = Batch.objects.exclude(pk=self.batch.pk).get()
        self.assertEqual(batch.lines.count(), 1)
        self.assertIn("Local extraction", batch.parser_notes)
        self.assertTrue(batch.source_file.storage.exists(batch.source_file.name))
        self.assertFalse(Container.objects.exists())
