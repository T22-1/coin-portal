from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from reportlab.lib.units import inch

from .models import InventoryItem
from .views import LABEL_BARCODE_MAX_WIDTH, _fit_code128


class PortalSmokeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin", password="admin12345")

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
        barcode = _fit_code128("ID-76519140911", LABEL_BARCODE_MAX_WIDTH, 0.006 * inch, 0.0035 * inch)

        self.assertLessEqual(barcode.width, LABEL_BARCODE_MAX_WIDTH)

    def test_sale_batch_accepts_generated_item_ids(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create()

        response = self.client.post(reverse("sale_add_scan"), {"code": item.internal_id})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["sale_batch"], [item.internal_id])
