from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import InventoryItem


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

    def test_sale_batch_accepts_generated_item_ids(self):
        self.client.force_login(self.user)
        item = InventoryItem.objects.create()

        response = self.client.post(reverse("sale_add_scan"), {"code": item.internal_id})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["sale_batch"], [item.internal_id])
