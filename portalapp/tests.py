from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.utils import DatabaseError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from decimal import Decimal
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from reportlab.lib.units import inch
from pypdf import PdfReader

from .models import Container, CrackoutEvent, IncomingInventoryBatch, InventoryItem, PricingPlan, Product, Sale, SaleItem, SaleTube, Submission, SubmissionItem
from .views import LABEL_BUSINESS_NAME, LABEL_MARGIN_X, LABEL_WIDTH, _fit_code128, _pcgs_submission_number, _submission_form_number


def pdf_annotation_values(response):
    reader = PdfReader(BytesIO(response.content))
    values = {}
    for page in reader.pages:
        annotations = page.get("/Annots")
        if not annotations:
            continue
        for annotation_ref in annotations.get_object():
            annotation = annotation_ref.get_object()
            field_name = annotation.get("/T")
            if field_name:
                values[str(field_name)] = annotation.get("/V")
    return values


class PortalSmokeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="admin",
            password="admin12345",
            is_staff=True,
            is_superuser=True,
        )

    def test_login_page_loads(self):
        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Coin Portal Login")
        self.assertNotContains(response, "admin12345")

    def test_home_shows_pricing_when_signed_out(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CoinPortal 365 Pricing")

    def test_pricing_shows_when_signed_out(self):
        response = self.client.get(reverse("pricing"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CoinPortal 365 Pricing")

    def test_dashboard_redirects_to_login_when_signed_out(self):
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_pricing_page_shows_public_active_plans(self):
        self.client.force_login(self.user)
        PricingPlan.objects.create(
            name="Dealer Pro",
            slug="dealer-pro",
            tagline="Built for active coin dealers.",
            price="149.00",
            stripe_price_id="price_test_123",
            feature_bullets="Inventory\nSubmission forms\nSales workflow",
            is_featured=True,
        )
        PricingPlan.objects.create(
            name="Hidden",
            slug="hidden",
            price="1.00",
            is_public=False,
        )

        response = self.client.get(reverse("pricing"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dealer Pro")
        self.assertContains(response, "price_test_123")
        self.assertNotContains(response, "Hidden")

    def test_pricing_plan_is_hidden_from_admin_index(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Pricing plans")

    def test_sale_line_models_are_hidden_from_admin_index(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sales")
        self.assertNotContains(response, "Sale items")
        self.assertNotContains(response, "Sale tubes")

    def test_incoming_inventory_batches_plural_is_spelled_correctly(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Incoming inventory batches")
        self.assertNotContains(response, "Incoming inventory batchs")

    def test_admin_reports_tab_opens_report_chooser(self):
        self.client.force_login(self.user)

        index_response = self.client.get(reverse("admin:index"))
        reports_response = self.client.get(reverse("admin:portalapp_report_changelist"))

        self.assertEqual(index_response.status_code, 200)
        self.assertContains(index_response, "Reports")
        self.assertEqual(reports_response.status_code, 200)
        self.assertContains(reports_response, "Choose the report area you want to open.")
        self.assertContains(reports_response, "Inventory")
        self.assertContains(reports_response, "Tubes")
        self.assertContains(reports_response, "Products")
        self.assertContains(reports_response, "Submissions")
        self.assertContains(reports_response, "Sales")
        self.assertContains(reports_response, "Profit &amp; Loss")
        self.assertContains(reports_response, reverse("admin:portalapp_report_detail", kwargs={"report_type": "inventory"}))
        self.assertContains(reports_response, reverse("admin:portalapp_report_detail", kwargs={"report_type": "tubes"}))
        self.assertContains(reports_response, reverse("admin:portalapp_report_detail", kwargs={"report_type": "products"}))

    def test_admin_product_report_shows_quantity_summary(self):
        self.client.force_login(self.user)
        Product.objects.create(name="Storage Boxes", sku="BOX-100", quantity=500, unit_price="2.50")
        Product.objects.create(name="Display Stands", sku="STAND-25", quantity=25, unit_price="5.00")

        response = self.client.get(reverse("admin:portalapp_report_detail", kwargs={"report_type": "products"}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Products Report")
        self.assertContains(response, "Total products")
        self.assertContains(response, "Total quantity")
        self.assertContains(response, "525")
        self.assertContains(response, "Total value")
        self.assertContains(response, "$1,375.00")
        self.assertContains(response, "Aging Summary")
        self.assertContains(response, "Storage Boxes")
        self.assertContains(response, "BOX-100")

    def test_admin_inventory_report_opens_report_page(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(
            internal_id="ID-REPORT-001",
            date_mm="1889",
            denomination="1c",
            series="Indian Head Cent",
            holder="PCGS",
            grade_text="PR66BN",
            cert_number="51076687",
            ask_price="2000.00",
            cost_basis="1200.00",
        )
        InventoryItem.objects.filter(pk=item.pk).update(created_at=timezone.now() - timedelta(days=95))

        response = self.client.get(reverse("admin:portalapp_report_detail", kwargs={"report_type": "inventory"}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Inventory Report")
        self.assertContains(response, "Total items")
        self.assertContains(response, "Total ask value")
        self.assertContains(response, "$2,000.00")
        self.assertContains(response, "Total cost")
        self.assertContains(response, "$1,200.00")
        self.assertContains(response, "Potential gross")
        self.assertContains(response, "$800.00")
        self.assertContains(response, "Aging Summary")
        self.assertContains(response, "91+ days")
        self.assertContains(response, "ID-REPORT-001")
        self.assertContains(response, "51076687")
        self.assertContains(response, reverse("admin:portalapp_report_export", kwargs={"report_type": "inventory", "export_format": "csv"}))
        self.assertContains(response, reverse("admin:portalapp_report_export", kwargs={"report_type": "inventory", "export_format": "xlsx"}))
        self.assertContains(response, reverse("admin:portalapp_report_export", kwargs={"report_type": "inventory", "export_format": "pdf"}))

    def test_admin_report_exports_csv_xlsx_and_pdf(self):
        self.client.force_login(self.user)
        InventoryItem.objects.create(internal_id="ID-EXPORT-001", date_mm="1889", denomination="1c")

        csv_response = self.client.get(
            reverse("admin:portalapp_report_export", kwargs={"report_type": "inventory", "export_format": "csv"})
        )
        xlsx_response = self.client.get(
            reverse("admin:portalapp_report_export", kwargs={"report_type": "inventory", "export_format": "xlsx"})
        )
        pdf_response = self.client.get(
            reverse("admin:portalapp_report_export", kwargs={"report_type": "inventory", "export_format": "pdf"})
        )

        self.assertEqual(csv_response.status_code, 200)
        self.assertEqual(csv_response["Content-Type"], "text/csv")
        self.assertIn("ID-EXPORT-001", csv_response.content.decode())
        self.assertEqual(xlsx_response.status_code, 200)
        self.assertEqual(
            xlsx_response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertTrue(xlsx_response.content.startswith(b"PK"))
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response["Content-Type"], "application/pdf")
        self.assertTrue(pdf_response.content.startswith(b"%PDF"))

    def test_admin_profit_loss_report_shows_revenue_cost_and_profit(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(
            internal_id="ID-PL-001",
            date_mm="1950-D",
            denomination="50c",
            series="Franklin Half Dollar",
            cost_basis="900.00",
        )
        sale = Sale.objects.create()
        SaleItem.objects.create(sale=sale, item=item, sold_price="1500.00")

        response = self.client.get(reverse("admin:portalapp_report_detail", kwargs={"report_type": "profit-loss"}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Profit &amp; Loss Report")
        self.assertContains(response, "Revenue")
        self.assertContains(response, "$1,500.00")
        self.assertContains(response, "Cost basis")
        self.assertContains(response, "$900.00")
        self.assertContains(response, "Gross profit")
        self.assertContains(response, "$600.00")
        self.assertContains(response, "40.0%")

    def test_product_admin_tab_supports_quantity_products(self):
        self.client.force_login(self.user)
        product = Product.objects.create(name="Storage Boxes", sku="BOX-100", quantity=500, unit_price="2.50")

        index_response = self.client.get(reverse("admin:index"))
        list_response = self.client.get(reverse("admin:portalapp_product_changelist"))
        add_response = self.client.get(reverse("admin:portalapp_product_add"))

        self.assertEqual(index_response.status_code, 200)
        self.assertContains(index_response, "Products")
        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, product.internal_id)
        self.assertContains(list_response, "Storage Boxes")
        self.assertContains(list_response, "500")
        self.assertEqual(add_response.status_code, 200)
        self.assertContains(add_response, "Leave blank to generate automatically.")

    def test_tube_admin_add_page_explains_auto_internal_id(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("admin:portalapp_container_add"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Leave blank to generate automatically.")
        self.assertContains(response, "Date / Mint Mark")
        self.assertContains(response, "Denomination")
        self.assertContains(response, "Series")
        self.assertContains(response, "portalapp/admin_inventory_actions.js")
        self.assertRegex(
            response.content.decode(),
            r'id="id_quantity"[^>]*value="1"|value="1"[^>]*id="id_quantity"',
        )

    def test_tube_default_quantity_is_one(self):
        tube = Container.objects.create()

        self.assertEqual(tube.quantity, 1)

    def test_tube_save_infers_series_and_label_text(self):
        tube = Container.objects.create(date_mm="1950-D", denomination="50c", quantity=20)

        self.assertEqual(tube.series, "Franklin Half Dollar")
        self.assertEqual(tube.label_text, "1950-D 50c Franklin Half Dollar QTY 20")

    def test_tube_admin_series_autofill_runs_while_typing(self):
        script = Path("portalapp/static/portalapp/admin_inventory_actions.js").read_text()

        self.assertIn("function bindAutoEvents", script)
        self.assertIn("'input'", script)
        self.assertIn("bindAutoEvents(denominationInput, fillSeriesIfAutoManaged)", script)
        self.assertIn("bindAutoEvents(dateInput, fillSeriesIfAutoManaged)", script)
        self.assertIn("fillSeriesIfAutoManaged();", script)

    def test_inventory_admin_add_page_uses_coin_friendly_labels(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("admin:portalapp_inventoryitem_add"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Leave blank to generate automatically.")
        self.assertContains(response, "Date / Mint Mark")
        self.assertContains(response, "Grading Company")
        self.assertContains(response, "Grade")
        self.assertContains(response, "Cert Number")
        self.assertContains(response, "CAC Sticker")
        self.assertNotContains(response, "Cacg holder")
        content = response.content.decode()
        self.assertLess(content.index("Date / Mint Mark"), content.index("Series"))
        self.assertLess(content.index("Date / Mint Mark"), content.index("Denomination"))
        self.assertLess(content.index("Denomination"), content.index("Series"))
        self.assertLess(content.index("Series"), content.index("Grading Company"))
        self.assertLess(content.index("Grading Company"), content.index("Grade"))
        self.assertLess(content.index("Acquired date"), content.index("Created at"))
        self.assertContains(response, "portalapp/admin_inventory_actions.js")

    def test_inventory_admin_changelist_shows_status_subsections(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("admin:portalapp_inventoryitem_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "inventory-status-tabs")
        self.assertContains(response, "In Stock")
        self.assertContains(response, "At Grading")
        self.assertContains(response, "Sold")
        self.assertContains(response, ".?status__exact=AT_GRADING")

    def test_tube_admin_changelist_shows_stock_and_sold_subsections(self):
        self.client.force_login(self.user)
        available_tube = Container.objects.create(internal_id="TUBE-AVAILABLE", label_text="Available tube")
        sold_tube = Container.objects.create(internal_id="TUBE-SOLD", label_text="Sold tube")
        sale = Sale.objects.create()
        SaleTube.objects.create(sale=sale, tube=sold_tube, sold_price="100.00")

        response = self.client.get(reverse("admin:portalapp_container_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "inventory-status-tabs")
        self.assertContains(response, "In Stock")
        self.assertContains(response, ".?sold_status=in_stock")
        self.assertContains(response, "Sold")
        self.assertContains(response, ".?sold_status=sold")
        self.assertContains(response, available_tube.internal_id)
        self.assertContains(response, sold_tube.internal_id)

        stock_response = self.client.get(reverse("admin:portalapp_container_changelist"), {"sold_status": "in_stock"})

        self.assertEqual(stock_response.status_code, 200)
        self.assertContains(stock_response, available_tube.internal_id)
        self.assertNotContains(stock_response, sold_tube.internal_id)

        sold_response = self.client.get(reverse("admin:portalapp_container_changelist"), {"sold_status": "sold"})

        self.assertEqual(sold_response.status_code, 200)
        self.assertNotContains(sold_response, available_tube.internal_id)
        self.assertContains(sold_response, sold_tube.internal_id)

    def test_inventory_master_list_searches_core_coin_fields(self):
        self.client.force_login(self.user)
        matched = InventoryItem.objects.create(
            internal_id="ID-MASTER-001",
            date_mm="1889",
            denomination="1c",
            series="Indian Head Cent",
            holder="PCGS",
            grade_text="PR66BN",
            cert_number="51076687",
            ask_price="1000.00",
        )
        InventoryItem.objects.create(internal_id="ID-MASTER-002", date_mm="1939", holder="CACG")

        response = self.client.get(reverse("inventory_master_list"), {"q": "51076687"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, matched.internal_id)
        self.assertContains(response, "Indian Head Cent")
        self.assertNotContains(response, "ID-MASTER-002")

    def test_inventory_master_list_shows_status_subsections(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory_master_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="/inventory/"')
        self.assertContains(response, 'href="/inventory/?status=IN_STOCK"')
        self.assertContains(response, "In Stock")
        self.assertContains(response, 'href="/inventory/?status=AT_GRADING"')
        self.assertContains(response, "At Grading")
        self.assertContains(response, 'href="/inventory/?status=SOLD"')
        self.assertContains(response, "Sold")

    def test_inventory_and_tube_auto_ids_use_separate_six_digit_ranges(self):
        item = InventoryItem.objects.create()
        tube = Container.objects.create()

        self.assertEqual(item.internal_id, "ID-281947")
        self.assertEqual(tube.internal_id, "TUBE-864203")

    def test_submission_auto_ids_use_distinct_seven_digit_range(self):
        submission = Submission.objects.create(service="PCGS")

        self.assertEqual(submission.internal_id, "SUB-9263841")

    def test_inventory_and_tube_auto_ids_increment_from_existing_new_ranges(self):
        InventoryItem.objects.create(internal_id="ID-281947")
        Container.objects.create(internal_id="TUBE-864203")

        item = InventoryItem.objects.create()
        tube = Container.objects.create()

        self.assertEqual(item.internal_id, "ID-281948")
        self.assertEqual(tube.internal_id, "TUBE-864204")

    def test_inventory_and_tube_auto_ids_skip_collisions(self):
        InventoryItem.objects.create(internal_id="ID-281947")
        InventoryItem.objects.create(internal_id="ID-281948")
        Container.objects.create(internal_id="TUBE-864203")
        Container.objects.create(internal_id="TUBE-864204")

        item = InventoryItem.objects.create()
        tube = Container.objects.create()

        self.assertEqual(item.internal_id, "ID-281949")
        self.assertEqual(tube.internal_id, "TUBE-864205")

    def test_sale_auto_ids_use_seven_digit_random_range(self):
        sale = Sale.objects.create()

        self.assertEqual(sale.internal_id, "SALE-7384921")

    def test_sale_auto_ids_increment_and_skip_collisions(self):
        Sale.objects.create(internal_id="SALE-7384921")
        Sale.objects.create(internal_id="SALE-7384922")

        sale = Sale.objects.create()

        self.assertEqual(sale.internal_id, "SALE-7384923")

    def test_incoming_inventory_upload_stages_rows_for_review(self):
        self.client.force_login(self.user)
        invoice = SimpleUploadedFile(
            "invoice.csv",
            b"description,date,denom,series,holder,grade,cert,cost\n"
            b"1889 1c Indian Head Cent PCGS PR66BN 51076687,1889,1c,Indian Head Cent,PCGS,PR66BN,51076687,1000.00\n",
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("incoming_inventory_upload"),
            {"vendor": "Test Dealer", "invoice": invoice},
        )

        batch = IncomingInventoryBatch.objects.get()
        line = batch.lines.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(batch.parser_status, "PARSED")
        self.assertEqual(line.date_mm, "1889")
        self.assertEqual(line.denomination, "1c")
        self.assertEqual(line.series, "Indian Head Cent")
        self.assertFalse(line.needs_review)

    def test_incoming_inventory_review_imports_selected_ready_rows(self):
        self.client.force_login(self.user)
        batch = IncomingInventoryBatch.objects.create(title="Test Intake", vendor="Show Dealer")
        line = batch.lines.create(
            raw_description="1889 1c Indian Head Cent PCGS PR66BN 51076687",
            date_mm="1889",
            denomination="1c",
            series="Indian Head Cent",
            holder="PCGS",
            grade_text="PR66BN",
            cert_number="51076687",
            cost_basis="1000.00",
            needs_review=False,
            confidence=95,
        )

        response = self.client.post(
            reverse("incoming_inventory_batch", args=[batch.id]),
            {
                "action": "import",
                "selected_lines": [str(line.id)],
                f"line_{line.id}_raw_description": line.raw_description,
                f"line_{line.id}_date_mm": line.date_mm,
                f"line_{line.id}_denomination": line.denomination,
                f"line_{line.id}_series": line.series,
                f"line_{line.id}_variety": "",
                f"line_{line.id}_holder": line.holder,
                f"line_{line.id}_grade_text": line.grade_text,
                f"line_{line.id}_cert_number": line.cert_number,
                f"line_{line.id}_cost_basis": "1000.00",
                f"line_{line.id}_ask_price": "",
                f"line_{line.id}_source": "Show Dealer",
            },
        )

        line.refresh_from_db()
        item = InventoryItem.objects.get(cert_number="51076687")
        batch.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(line.imported_item, item)
        self.assertEqual(item.source, "Show Dealer")
        self.assertEqual(batch.parser_status, "IMPORTED")

    def test_inventory_master_list_filters_by_holder_and_status(self):
        self.client.force_login(self.user)
        InventoryItem.objects.create(internal_id="ID-FILTER-001", holder="PCGS", status="IN_STOCK")
        InventoryItem.objects.create(internal_id="ID-FILTER-002", holder="NGC", status="SOLD")

        response = self.client.get(reverse("inventory_master_list"), {"holder": "PCGS", "status": "IN_STOCK"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ID-FILTER-001")
        self.assertNotContains(response, "ID-FILTER-002")

    def test_active_submissions_page_lists_only_active_submissions(self):
        self.client.force_login(self.user)
        active = Submission.objects.create(internal_id="SUB-ACTIVE-LIST", service="PCGS", status="AT_GRADING")
        inactive = Submission.objects.create(internal_id="SUB-INACTIVE-LIST", service="NGC", status="RETURNED")
        item = InventoryItem.objects.create(internal_id="ID-ACTIVE-LIST")
        SubmissionItem.objects.create(submission=active, item=item)

        response = self.client.get(reverse("active_submissions"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SUB-ACTIVE-LIST")
        self.assertContains(response, "Open Packet")
        self.assertNotContains(response, inactive.internal_id)

    def test_item_label_pdf_renders(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(
            denomination="$1",
            series="Morgan Dollar",
            date_mm="1881-S",
            holder="PCGS",
            grade_text="MS65",
            ask_price="2450.00",
        )

        response = self.client.get(reverse("label_item_pdf", kwargs={"code": item.internal_id}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertIn(b"/MediaBox [ 0 0 144 54 ]", response.content)
        text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(response.content)).pages)
        self.assertIn(LABEL_BUSINESS_NAME, text)
        self.assertIn("ASK $2,450.00", text)

    def test_item_label_pdf_handles_long_internal_ids(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(
            internal_id="ID-76519140911",
            denomination="50C",
            date_mm="1939",
            holder="PCGS",
            grade_text="PR67+",
            ask_price="2500.00",
        )

        response = self.client.get(reverse("label_item_pdf", kwargs={"code": item.internal_id}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_tube_label_pdf_renders(self):
        self.client.force_login(self.user)
        tube = Container.objects.create(
            internal_id="TUBE-1950",
            date_mm="1943-D",
            denomination="1c",
            quantity=50,
            ask_price="200.00",
        )

        response = self.client.get(reverse("label_tube_pdf", kwargs={"code": tube.internal_id}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
        text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(response.content)).pages)
        self.assertIn("TUBE-1950", text)
        self.assertIn("1943-D 1c Lincoln Cent QTY 50", text)
        self.assertIn(LABEL_BUSINESS_NAME, text)
        self.assertIn("ASK $200.00", text)

    def test_long_id_barcode_stays_inside_printable_area(self):
        printable_width = LABEL_WIDTH - (2 * LABEL_MARGIN_X)
        barcode = _fit_code128("ID-76519140911", printable_width, 0.0078 * inch, 0.0045 * inch)

        self.assertLessEqual(barcode.width, printable_width)

    def test_sale_batch_accepts_generated_item_ids(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create()

        response = self.client.post(reverse("sale_add_scan"), {"code": item.internal_id})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["sale_batch"], [item.internal_id])

    def test_sale_batch_accepts_raw_numeric_item_and_tube_codes(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(internal_id="ID-281947")
        tube = Container.objects.create(internal_id="TUBE-864203", label_text="BU roll")

        item_response = self.client.post(reverse("sale_add_scan"), {"code": "281947"})
        tube_response = self.client.post(reverse("sale_add_scan"), {"code": "864203"})

        self.assertEqual(item_response.status_code, 302)
        self.assertEqual(tube_response.status_code, 302)
        self.assertEqual(self.client.session["sale_batch"], [item.internal_id, tube.internal_id])

    def test_sale_batch_warns_when_code_is_not_found(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse("sale_add_scan"), {"code": "123456"}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session.get("sale_batch", []), [])
        self.assertContains(response, "123456 was not found.")

    def test_sale_batch_prefills_backend_prices(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(ask_price="1250.00")
        tube = Container.objects.create(label_text="BU roll", ask_price="200.00")
        session = self.client.session
        session["sale_batch"] = [item.internal_id, tube.internal_id]
        session.save()

        response = self.client.get(reverse("sale_batch"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="1250.00"')
        self.assertContains(response, 'value="200.00"')
        self.assertContains(response, 'formaction="/sale/remove/"')

    def test_sale_batch_remove_deletes_one_code_from_session(self):
        self.client.force_login(self.user)
        first = InventoryItem.objects.create()
        second = InventoryItem.objects.create()
        session = self.client.session
        session["sale_batch"] = [first.internal_id, second.internal_id]
        session.save()

        response = self.client.post(reverse("sale_remove_scan"), {"code": first.internal_id})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["sale_batch"], [second.internal_id])

    def test_sale_batch_rejects_items_at_grading(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(status="AT_GRADING")

        response = self.client.post(reverse("sale_add_scan"), {"code": item.internal_id})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session.get("sale_batch", []), [])

    def test_sale_complete_records_items_tubes_and_generates_invoice(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(
            date_mm="1889",
            denomination="1c",
            series="Indian Head Cent",
            holder="PCGS",
            grade_text="PR66BN",
            cert_number="51076687",
        )
        tube = Container.objects.create(label_text="1943-D BU Qty 50", quantity=50)
        session = self.client.session
        session["sale_batch"] = [item.internal_id, tube.internal_id]
        session.save()

        response = self.client.post(
            reverse("sale_complete"),
            {
                "venue": "Show",
                f"price_{item.internal_id}": "1000",
                f"price_{tube.internal_id}": "200",
            },
        )

        sale = Sale.objects.get()
        self.assertRedirects(response, reverse("sale_invoice_pdf", kwargs={"sale_id": sale.pk}), fetch_redirect_response=False)
        item.refresh_from_db()
        self.assertEqual(item.status, "SOLD")
        self.assertEqual(sale.lines.get().sold_price, Decimal("1000"))
        self.assertEqual(SaleTube.objects.get().sold_price, Decimal("200"))

        invoice = self.client.get(reverse("sale_invoice_pdf", kwargs={"sale_id": sale.pk}))
        text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(invoice.content)).pages)

        self.assertEqual(invoice.status_code, 200)
        self.assertIn("TMC Marketplace, Inc.", text)
        self.assertIn("1 Chase Corporate Drive", text)
        self.assertIn("Suite 400", text)
        self.assertIn("Birmingham, AL 35244", text)
        self.assertIn("Cert Number", text)
        self.assertIn("51076687", text)
        self.assertIn("N/A", text)
        self.assertIn(item.internal_id, text)
        self.assertIn(tube.internal_id, text)
        self.assertIn("$1,200.00", text)

    def test_sale_complete_uses_backend_prices_when_post_prices_are_blank(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(ask_price="1250.00")
        tube = Container.objects.create(label_text="BU roll", ask_price="200.00")
        session = self.client.session
        session["sale_batch"] = [item.internal_id, tube.internal_id]
        session.save()

        self.client.post(
            reverse("sale_complete"),
            {
                f"price_{item.internal_id}": "",
                f"price_{tube.internal_id}": "",
            },
        )

        sale = Sale.objects.get()
        self.assertEqual(sale.lines.get().sold_price, Decimal("1250.00"))
        self.assertEqual(sale.tube_lines.get().sold_price, Decimal("200.00"))

    def test_submission_admin_pages_load(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create()
        submission = Submission.objects.create(service="PCGS")
        SubmissionItem.objects.create(submission=submission, item=item)

        submission_response = self.client.get(reverse("admin:portalapp_submission_changelist"))
        line_response = self.client.get(reverse("admin:portalapp_submissionitem_changelist"))

        self.assertEqual(submission_response.status_code, 200)
        self.assertEqual(line_response.status_code, 200)

    def test_submission_admin_changelists_use_stable_columns(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create()
        submission = Submission.objects.create(service="PCGS")
        SubmissionItem.objects.create(submission=submission, item=item)

        with CaptureQueriesContext(connection) as captured:
            self.client.get(reverse("admin:portalapp_submission_changelist"))
            self.client.get(reverse("admin:portalapp_submissionitem_changelist"))

        sql = "\n".join(query["sql"] for query in captured.captured_queries)
        self.assertNotIn("grading_submission_number", sql)
        self.assertNotIn("submission_method", sql)
        self.assertNotIn("tracking_number", sql)
        self.assertNotIn("show_name", sql)

    def test_submission_item_admin_add_page_uses_stable_columns(self):
        self.client.force_login(self.user)
        InventoryItem.objects.create()
        Submission.objects.create(service="PCGS")

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("admin:portalapp_submissionitem_add"))

        self.assertEqual(response.status_code, 200)
        sql = "\n".join(query["sql"] for query in captured.captured_queries)
        self.assertNotIn("grading_submission_number", sql)
        self.assertNotIn("submission_method", sql)
        self.assertNotIn("tracking_number", sql)
        self.assertNotIn("show_name", sql)

    def test_submission_admin_add_page_uses_stable_fields(self):
        self.client.force_login(self.user)

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("admin:portalapp_submission_add"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Internal id")
        self.assertContains(response, "Service")
        self.assertContains(response, "Status")
        self.assertContains(response, "Notes")
        self.assertNotContains(response, "Tracking number")

        sql = "\n".join(query["sql"] for query in captured.captured_queries)
        self.assertNotIn("grading_submission_number", sql)
        self.assertNotIn("submission_method", sql)
        self.assertNotIn("tracking_number", sql)
        self.assertNotIn("show_name", sql)

    def test_submission_admin_add_page_saves_stable_fields(self):
        self.client.force_login(self.user)
        url = reverse("admin:portalapp_submission_add")

        response = self.client.post(
            url,
            {
                "internal_id": "SUB-SHOW-001",
                "service": "PCGS",
                "status": "PREPARED",
                "notes": "January show submission",
                "_save": "Save",
            },
            follow=False,
        )

        self.assertEqual(response.status_code, 302)
        submission = Submission.objects.only("internal_id", "service", "status", "notes").get(
            internal_id="SUB-SHOW-001"
        )
        self.assertEqual(submission.service, "PCGS")
        self.assertEqual(submission.status, "PREPARED")
        self.assertEqual(submission.notes, "January show submission")

    def test_crackout_event_admin_add_page_uses_stable_submission_columns(self):
        self.client.force_login(self.user)
        InventoryItem.objects.create()
        Submission.objects.create(service="PCGS")

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("admin:portalapp_crackoutevent_add"))

        self.assertEqual(response.status_code, 200)
        sql = "\n".join(query["sql"] for query in captured.captured_queries)
        self.assertNotIn("grading_submission_number", sql)
        self.assertNotIn("submission_method", sql)
        self.assertNotIn("tracking_number", sql)
        self.assertNotIn("show_name", sql)

    def test_crackout_event_admin_add_page_saves(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(internal_id="ID-CRACKOUT-001")
        submission = Submission.objects.create(service="PCGS")
        url = reverse("admin:portalapp_crackoutevent_add")

        response = self.client.post(
            url,
            {
                "item": item.id,
                "from_service": "CACG",
                "from_grade": "PR67+",
                "from_cert": "8991015409",
                "to_submission": submission.id,
                "reason": "Try for crossover",
                "outcome": "",
                "_save": "Save",
            },
            follow=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(CrackoutEvent.objects.filter(item=item, to_submission=submission).exists())

    def test_admin_batch_label_pdf_renders_selected_items(self):
        self.client.force_login(self.user)
        first = InventoryItem.objects.create(internal_id="ID-76519140911")
        second = InventoryItem.objects.create(internal_id="ID-76519140912")
        url = reverse("admin:portalapp_inventoryitem_print_labels")

        response = self.client.get(f"{url}?ids={first.id},{second.id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_admin_batch_label_pdf_renders_selected_tubes(self):
        self.client.force_login(self.user)
        first = Container.objects.create(internal_id="TUBE-1950", label_text="1943-D BU QTY 50")
        second = Container.objects.create(internal_id="TUBE-1951", label_text="Wheat cents")
        url = reverse("admin:portalapp_container_print_labels")

        response = self.client.get(f"{url}?ids={first.id},{second.id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
        reader = PdfReader(BytesIO(response.content))
        self.assertEqual(len(reader.pages), 2)

    def test_submission_packet_page_and_exports_render(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(
            internal_id="ID-PACKET-001",
            date_mm="1881-S",
            denomination="$1",
            series="Morgan Dollar",
            holder="PCGS",
            grade_text="MS65",
            cert_number="12345678",
            ask_price="250.00",
        )
        submission = Submission.objects.create(internal_id="SUB-PACKET-001", service="PCGS")
        SubmissionItem.objects.create(submission=submission, item=item, declared_value="250.00")

        page = self.client.get(reverse("submission_packet", kwargs={"submission_id": submission.id}))
        csv_response = self.client.get(reverse("submission_packet_csv", kwargs={"submission_id": submission.id}))
        pdf_response = self.client.get(reverse("submission_packet_pdf", kwargs={"submission_id": submission.id}))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "SUB-PACKET-001")
        self.assertContains(page, "ID-PACKET-001")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("ID-PACKET-001", csv_response.content.decode())
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response["Content-Type"], "application/pdf")
        self.assertTrue(pdf_response.content.startswith(b"%PDF"))

    def test_submission_pcgs_pdf_fills_template_fields(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(
            internal_id="ID-PCGS-001",
            date_mm="1881-S",
            denomination="$1",
            series="Morgan Dollar",
            holder="PCGS",
            grade_text="MS65",
            cert_number="12345678",
            ask_price="250.00",
        )
        submission = Submission.objects.create(
            internal_id="SUB-PCGS-001",
            service="PCGS",
        )
        SubmissionItem.objects.create(submission=submission, item=item, declared_value="250.00")

        response = self.client.get(reverse("submission_pcgs_pdf", kwargs={"submission_id": submission.id}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        fields = pdf_annotation_values(response)
        self.assertEqual(fields["SubmissionNumber"], _pcgs_submission_number(submission))
        self.assertEqual(fields["QTY1"], "1")
        self.assertEqual(fields.get("COIN NUMBER1", ""), "")
        self.assertEqual(fields["DATEMINT MARK1"], "1881-S")
        self.assertEqual(fields["DENOM1"], "$1")
        self.assertEqual(fields.get("COIN DESCRIPTIONVARIETY1", ""), "")
        self.assertEqual(fields.get("GRADEM_1", ""), "")
        self.assertEqual(fields.get("CERTIFICATION NUMBERM_1", ""), "")
        self.assertEqual(fields["DECLARED VALUE REQUIREDM_1"], "250.00")
        rendered_text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(response.content)).pages)
        self.assertIn(_pcgs_submission_number(submission), rendered_text)

    def test_submission_pcgs_pdf_generates_seven_digit_submission_number(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(internal_id="ID-PCGS-RANDOM")
        submission = Submission.objects.create(internal_id="SUB-PCGS-RANDOM", service="PCGS")
        SubmissionItem.objects.create(submission=submission, item=item)

        response = self.client.get(reverse("submission_pcgs_pdf", kwargs={"submission_id": submission.id}))

        self.assertEqual(response.status_code, 200)
        fields = pdf_annotation_values(response)
        self.assertRegex(fields["SubmissionNumber"], r"^\d{7}$")
        self.assertEqual(fields["SubmissionNumber"], _pcgs_submission_number(submission))

    def test_submission_ngc_pdf_fills_template_fields(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(
            internal_id="ID-NGC-001",
            date_mm="1939",
            denomination="50c",
            series="Walking Liberty Half Dollar",
            holder="NGC",
            grade_text="MS64",
            cert_number="8991015409",
            ask_price="2500.00",
        )
        submission = Submission.objects.create(internal_id="SUB-NGC-001", service="NGC")
        SubmissionItem.objects.create(submission=submission, item=item, declared_value="2500.00")

        response = self.client.get(reverse("submission_ngc_pdf", kwargs={"submission_id": submission.id}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        fields = PdfReader(BytesIO(response.content)).get_fields()
        self.assertEqual(fields["Invoice Number from NGC Submission"]["/V"], _submission_form_number(submission, "NGC"))
        self.assertEqual(fields["Qty 1"]["/V"], "1")
        self.assertEqual(fields["Country 1"]["/V"], "USA")
        self.assertEqual(fields["Coin Date 1"]["/V"], "1939")
        self.assertEqual(fields["Denomination1"]["/V"], "50c")
        self.assertEqual(fields["Declare Value1"]["/V"], "2500.00")
        self.assertEqual(fields["TotalCoins"]["/V"], "1")
        self.assertEqual(fields["TotalDeclaredValue"]["/V"], "2500.00")

    def test_cac_and_cacg_submission_pdfs_fill_fields(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(
            internal_id="ID-CAC-001",
            date_mm="1889",
            denomination="1c",
            series="Indian Head Cent",
            holder="PCGS",
            grade_text="PR66BN",
            cert_number="51076687",
            ask_price="1000.00",
        )
        for service, route_name in (("CAC", "submission_cac_pdf"), ("CACG", "submission_cacg_pdf")):
            submission = Submission.objects.create(internal_id=f"SUB-{service}-001", service=service)
            SubmissionItem.objects.create(submission=submission, item=item, declared_value="1000.00")

            response = self.client.get(reverse(route_name, kwargs={"submission_id": submission.id}))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.assertTrue(response.content.startswith(b"%PDF"))
            fields = PdfReader(BytesIO(response.content)).get_fields()
            if service == "CAC":
                self.assertEqual(fields["shipment_this_submission_number"]["/V"], "1")
                self.assertEqual(fields["shipment_total_submissions"]["/V"], "1")
                self.assertEqual(fields["coin_01_date"]["/V"], "1889")
                self.assertEqual(fields["coin_01_denom"]["/V"], "1c")
                self.assertEqual(fields["coin_01_grade"]["/V"], "PR66BN")
                self.assertEqual(fields["coin_01_service"]["/V"], "PCGS")
                self.assertEqual(fields["coin_01_variety"].get("/V", ""), "")
                self.assertEqual(fields["coin_01_cert_number"].get("/V", ""), "")
                self.assertEqual(fields["coin_01_declared_value"]["/V"], "1000.00")
            else:
                self.assertEqual(fields["coin_01_date"]["/V"], "1889")
                self.assertEqual(fields["coin_01_denom"]["/V"], "1c")
                self.assertEqual(fields["coin_01_description"].get("/V", ""), "")
                self.assertEqual(fields["coin_01_current_grade"]["/V"], "PR66BN")
                self.assertEqual(fields["coin_01_cert_number"].get("/V", ""), "")
                self.assertEqual(fields["coin_01_declared_value"]["/V"], "1000.00")

    def test_submission_packet_add_scan_adds_items(self):
        self.client.force_login(self.user)
        submission = Submission.objects.create(internal_id="SUB-SCAN-001", service="PCGS")
        first = InventoryItem.objects.create(internal_id="ID-SCAN-001", ask_price="125.00")
        second = InventoryItem.objects.create(internal_id="ID-SCAN-002", cost_basis="80.00")

        response = self.client.post(
            reverse("submission_add_scan", kwargs={"submission_id": submission.id}),
            {"codes": f"{first.internal_id}\n{second.internal_id}"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(SubmissionItem.objects.filter(submission=submission).count(), 2)
        self.assertEqual(
            str(SubmissionItem.objects.get(submission=submission, item=first).declared_value),
            "125.00",
        )
        self.assertEqual(
            str(SubmissionItem.objects.get(submission=submission, item=second).declared_value),
            "80.00",
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, "AT_GRADING")
        self.assertEqual(second.status, "AT_GRADING")

    def test_submission_packet_add_scan_extracts_ids_from_pasted_links(self):
        self.client.force_login(self.user)
        submission = Submission.objects.create(internal_id="SUB-SCAN-LINKS", service="PCGS")
        first = InventoryItem.objects.create(internal_id="ID-547721")
        second = InventoryItem.objects.create(internal_id="ID-547722")

        response = self.client.post(
            reverse("submission_add_scan", kwargs={"submission_id": submission.id}),
            {
                "codes": (
                    "I am trying to add [**ID-547721**](https://example.com/item/11/change/) "
                    "**and** [**ID-547722**](https://example.com/item/12/change/)"
                )
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(SubmissionItem.objects.filter(submission=submission, item=first).exists())
        self.assertTrue(SubmissionItem.objects.filter(submission=submission, item=second).exists())

    def test_submission_packet_add_scan_skips_duplicates(self):
        self.client.force_login(self.user)
        submission = Submission.objects.create(internal_id="SUB-SCAN-002", service="NGC")
        item = InventoryItem.objects.create(internal_id="ID-SCAN-DUPE")
        SubmissionItem.objects.create(submission=submission, item=item)

        response = self.client.post(
            reverse("submission_add_scan", kwargs={"submission_id": submission.id}),
            {"codes": f"{item.internal_id}\n{item.internal_id}"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(SubmissionItem.objects.filter(submission=submission, item=item).count(), 1)

    def test_submission_packet_add_scan_handles_existing_duplicate_lines(self):
        self.client.force_login(self.user)
        submission = Submission.objects.create(internal_id="SUB-SCAN-DUPE-LINES", service="PCGS")
        item = InventoryItem.objects.create(internal_id="ID-547721")
        SubmissionItem.objects.create(submission=submission, item=item)
        SubmissionItem.objects.create(submission=submission, item=item)

        response = self.client.post(
            reverse("submission_add_scan", kwargs={"submission_id": submission.id}),
            {"codes": item.internal_id},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(SubmissionItem.objects.filter(submission=submission, item=item).count(), 2)
        self.assertContains(response, "already in this submission")

    def test_submission_packet_add_scan_handles_unexpected_item_error(self):
        self.client.force_login(self.user)
        submission = Submission.objects.create(internal_id="SUB-SCAN-ERROR", service="PCGS")
        item = InventoryItem.objects.create(internal_id="ID-547722")

        with patch("portalapp.views._submission_rejection_reason", side_effect=RuntimeError("boom")):
            response = self.client.post(
                reverse("submission_add_scan", kwargs={"submission_id": submission.id}),
                {"codes": item.internal_id},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(SubmissionItem.objects.filter(submission=submission, item=item).exists())
        self.assertContains(response, "ID-547722 could not be added")

    def test_submission_packet_add_scan_falls_back_when_normal_insert_fails(self):
        self.client.force_login(self.user)
        submission = Submission.objects.create(internal_id="SUB-SCAN-FALLBACK", service="PCGS")
        item = InventoryItem.objects.create(internal_id="ID-547723")

        with patch("portalapp.views.SubmissionItem.objects.create", side_effect=DatabaseError("forced failure")):
            response = self.client.post(
                reverse("submission_add_scan", kwargs={"submission_id": submission.id}),
                {"codes": item.internal_id},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(SubmissionItem.objects.filter(submission=submission, item=item).exists())
        self.assertContains(response, "Added 1 coin")

    def test_submission_packet_add_scan_rejects_item_on_another_active_submission(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(internal_id="ID-SCAN-ACTIVE")
        first = Submission.objects.create(internal_id="SUB-ACTIVE-001", service="PCGS", status="AT_GRADING")
        second = Submission.objects.create(internal_id="SUB-ACTIVE-002", service="NGC")
        SubmissionItem.objects.create(submission=first, item=item)

        response = self.client.post(
            reverse("submission_add_scan", kwargs={"submission_id": second.id}),
            {"codes": item.internal_id},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(SubmissionItem.objects.filter(item=item).count(), 1)
        self.assertContains(response, "already on active submission SUB-ACTIVE-001")

    def test_submission_packet_add_scan_active_lookup_uses_stable_submission_columns(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(internal_id="ID-SCAN-STABLE")
        first = Submission.objects.create(internal_id="SUB-STABLE-001", service="PCGS", status="AT_GRADING")
        second = Submission.objects.create(internal_id="SUB-STABLE-002", service="NGC")
        SubmissionItem.objects.create(submission=first, item=item)

        with CaptureQueriesContext(connection) as captured:
            response = self.client.post(
                reverse("submission_add_scan", kwargs={"submission_id": second.id}),
                {"codes": item.internal_id},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        sql = "\n".join(query["sql"] for query in captured.captured_queries)
        self.assertNotRegex(sql, r'SELECT .*"portalapp_submission"\."grading_submission_number"')
        self.assertNotRegex(sql, r'SELECT .*"portalapp_submission"\."submission_method"')
        self.assertNotRegex(sql, r'SELECT .*"portalapp_submission"\."tracking_number"')
        self.assertNotRegex(sql, r'SELECT .*"portalapp_submission"\."show_name"')

    def test_submission_packet_add_scan_moves_item_from_another_prepared_submission(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(internal_id="ID-547721")
        old_submission = Submission.objects.create(internal_id="SUB-PREPARED-OLD", service="PCGS", status="PREPARED")
        new_submission = Submission.objects.create(internal_id="SUB-PREPARED-NEW", service="PCGS", status="PREPARED")
        SubmissionItem.objects.create(submission=old_submission, item=item)

        response = self.client.post(
            reverse("submission_add_scan", kwargs={"submission_id": new_submission.id}),
            {"codes": item.internal_id},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(SubmissionItem.objects.filter(submission=old_submission, item=item).exists())
        self.assertTrue(SubmissionItem.objects.filter(submission=new_submission, item=item).exists())
        self.assertContains(response, "Added 1 coin")

    def test_submission_packet_add_scan_allows_item_from_inactive_submission(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(internal_id="ID-SCAN-INACTIVE")
        old_submission = Submission.objects.create(internal_id="SUB-INACTIVE-001", service="PCGS", status="RETURNED")
        new_submission = Submission.objects.create(internal_id="SUB-INACTIVE-002", service="NGC")
        SubmissionItem.objects.create(submission=old_submission, item=item)

        response = self.client.post(
            reverse("submission_add_scan", kwargs={"submission_id": new_submission.id}),
            {"codes": item.internal_id},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(SubmissionItem.objects.filter(submission=new_submission, item=item).exists())

    def test_submission_packet_add_scan_rejects_raw_coin_for_cac(self):
        self.client.force_login(self.user)
        submission = Submission.objects.create(internal_id="SUB-CAC-RAW", service="CAC")
        item = InventoryItem.objects.create(internal_id="ID-CAC-RAW", holder="RAW")

        response = self.client.post(
            reverse("submission_add_scan", kwargs={"submission_id": submission.id}),
            {"codes": item.internal_id},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(SubmissionItem.objects.filter(submission=submission, item=item).exists())
        self.assertContains(response, "cannot be added to CAC")

    def test_submission_packet_add_scan_allows_pcgs_or_ngc_coin_for_cac(self):
        self.client.force_login(self.user)
        submission = Submission.objects.create(internal_id="SUB-CAC-PCGS", service="CAC")
        item = InventoryItem.objects.create(internal_id="ID-CAC-PCGS", holder="PCGS")

        response = self.client.post(
            reverse("submission_add_scan", kwargs={"submission_id": submission.id}),
            {"codes": item.internal_id},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(SubmissionItem.objects.filter(submission=submission, item=item).exists())

    def test_submission_packet_remove_item_removes_line_and_restores_status(self):
        self.client.force_login(self.user)
        submission = Submission.objects.create(internal_id="SUB-REMOVE-001", service="PCGS")
        item = InventoryItem.objects.create(internal_id="ID-REMOVE-001", status="AT_GRADING")
        line = SubmissionItem.objects.create(submission=submission, item=item)

        response = self.client.post(
            reverse("submission_remove_item", kwargs={"submission_id": submission.id, "line_id": line.id})
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SubmissionItem.objects.filter(pk=line.id).exists())
        item.refresh_from_db()
        self.assertEqual(item.status, "IN_STOCK")

    def test_submission_packet_remove_item_keeps_status_if_item_is_on_another_submission(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(internal_id="ID-REMOVE-002", status="AT_GRADING")
        first = Submission.objects.create(internal_id="SUB-REMOVE-002", service="PCGS", status="RETURNED")
        second = Submission.objects.create(internal_id="SUB-REMOVE-003", service="NGC", status="RETURNED")
        line = SubmissionItem.objects.create(submission=first, item=item)
        SubmissionItem.objects.create(submission=second, item=item)

        response = self.client.post(
            reverse("submission_remove_item", kwargs={"submission_id": first.id, "line_id": line.id})
        )

        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.status, "AT_GRADING")

    def test_submission_admin_delete_removes_submission_and_restores_item_status(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create(internal_id="ID-DELETE-SUB", status="AT_GRADING")
        submission = Submission.objects.create(internal_id="SUB-DELETE-001", service="PCGS")
        SubmissionItem.objects.create(submission=submission, item=item)
        delete_url = reverse("admin:portalapp_submission_delete", kwargs={"object_id": submission.id})

        confirmation = self.client.get(delete_url)
        response = self.client.post(delete_url, {"post": "yes"}, follow=False)

        self.assertEqual(confirmation.status_code, 200)
        self.assertContains(confirmation, "Are you sure you want to delete submission")
        self.assertContains(confirmation, "SUB-DELETE-001")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Submission.objects.filter(id=submission.id).exists())
        self.assertFalse(SubmissionItem.objects.filter(submission_id=submission.id).exists())
        item.refresh_from_db()
        self.assertEqual(item.status, "IN_STOCK")

    def test_submission_admin_bulk_delete_removes_lines_and_restores_item_status(self):
        self.client.force_login(self.user)
        first_item = InventoryItem.objects.create(internal_id="ID-BULK-SUB-1", status="AT_GRADING")
        second_item = InventoryItem.objects.create(internal_id="ID-BULK-SUB-2", status="AT_GRADING")
        first_submission = Submission.objects.create(internal_id="SUB-BULK-001", service="PCGS")
        second_submission = Submission.objects.create(internal_id="SUB-BULK-002", service="NGC")
        SubmissionItem.objects.create(submission=first_submission, item=first_item)
        SubmissionItem.objects.create(submission=second_submission, item=second_item)

        delete_selected_url = reverse("admin:portalapp_submission_delete_selected")
        selected_ids = f"{first_submission.id},{second_submission.id}"
        confirmation = self.client.get(delete_selected_url, {"ids": selected_ids})
        response = self.client.post(delete_selected_url, {"ids": selected_ids, "post": "yes"}, follow=False)

        self.assertEqual(confirmation.status_code, 200)
        self.assertContains(confirmation, "Are you sure you want to delete these")
        self.assertContains(confirmation, "SUB-BULK-001")
        self.assertContains(confirmation, "SUB-BULK-002")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Submission.objects.filter(id__in=[first_submission.id, second_submission.id]).exists())
        self.assertFalse(SubmissionItem.objects.filter(submission_id__in=[first_submission.id, second_submission.id]).exists())
        first_item.refresh_from_db()
        second_item.refresh_from_db()
        self.assertEqual(first_item.status, "IN_STOCK")
        self.assertEqual(second_item.status, "IN_STOCK")
