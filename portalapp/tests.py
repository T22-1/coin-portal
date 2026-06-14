from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from reportlab.lib.units import inch

from .models import InventoryItem, Submission, SubmissionItem
from .views import LABEL_MARGIN_X, LABEL_WIDTH, _fit_code128


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

    def test_home_redirects_to_login_when_signed_out(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

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

    def test_admin_batch_label_pdf_renders_selected_items(self):
        self.client.force_login(self.user)
        first = InventoryItem.objects.create(internal_id="ID-76519140911")
        second = InventoryItem.objects.create(internal_id="ID-76519140912")
        url = reverse("admin:portalapp_inventoryitem_print_labels")

        response = self.client.get(f"{url}?ids={first.id},{second.id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
