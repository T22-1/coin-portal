from __future__ import annotations
import re
from django.db import models
from django.utils import timezone

CODE_STARTS = {
    "ID": 281947,
    "PROD": 4839201,
    "SALE": 7384921,
    "SUB": 9263841,
    "TUBE": 864203,
}

def _next_code(prefix: str, model_cls: type[models.Model], field_name: str = "internal_id") -> str:
    start = CODE_STARTS.get(prefix, 1950)
    prefix_marker = f"{prefix}-"
    values = model_cls.objects.filter(**{f"{field_name}__startswith": prefix_marker}).values_list(field_name, flat=True)
    highest = None
    for value in values:
        try:
            number = int(str(value).split("-")[-1])
        except (TypeError, ValueError):
            continue
        highest = number if highest is None else max(highest, number)
    n = max(highest + 1, start) if highest is not None else start
    while model_cls.objects.filter(**{field_name: f"{prefix}-{n}"}).exists():
        n += 1
    return f"{prefix}-{n}"


def _year_from_date_mint_mark(value: str) -> int | None:
    match = re.search(r"\b(17|18|19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


def _normalized_denomination(value: str) -> str:
    return (
        str(value or "")
        .lower()
        .replace(" ", "")
        .replace("cents", "c")
        .replace("cent", "c")
        .replace("dollars", "$")
        .replace("dollar", "$")
    )


def series_for_coin(date_mint_mark: str, denomination: str) -> str:
    year = _year_from_date_mint_mark(date_mint_mark)
    denom = _normalized_denomination(denomination)
    if not year or not denom:
        return ""

    if denom in ("1c", "c", "penny"):
        if 1859 <= year <= 1909:
            return "Indian Head Cent"
        if year >= 1909:
            return "Lincoln Cent"
    if denom in ("2c", "twoc") and 1864 <= year <= 1873:
        return "Two Cent Piece"
    if denom in ("3c", "threec") and 1851 <= year <= 1889:
        return "Three Cent Piece"
    if denom in ("5c", "nickel"):
        if 1866 <= year <= 1883:
            return "Shield Nickel"
        if 1883 <= year <= 1913:
            return "Liberty Head Nickel"
        if 1913 <= year <= 1938:
            return "Buffalo Nickel"
        if year >= 1938:
            return "Jefferson Nickel"
    if denom in ("10c", "dime"):
        if 1837 <= year <= 1891:
            return "Seated Liberty Dime"
        if 1892 <= year <= 1916:
            return "Barber Dime"
        if 1916 <= year <= 1945:
            return "Mercury Dime"
        if year >= 1946:
            return "Roosevelt Dime"
    if denom in ("20c", "twentyc") and 1875 <= year <= 1878:
        return "Twenty Cent Piece"
    if denom in ("25c", "quarter"):
        if 1838 <= year <= 1891:
            return "Seated Liberty Quarter"
        if 1892 <= year <= 1916:
            return "Barber Quarter"
        if 1916 <= year <= 1930:
            return "Standing Liberty Quarter"
        if year >= 1932:
            return "Washington Quarter"
    if denom in ("50c", "halfdollar", "half$"):
        if 1839 <= year <= 1891:
            return "Seated Liberty Half Dollar"
        if 1892 <= year <= 1915:
            return "Barber Half Dollar"
        if 1916 <= year <= 1947:
            return "Walking Liberty Half Dollar"
        if 1948 <= year <= 1963:
            return "Franklin Half Dollar"
        if year >= 1964:
            return "Kennedy Half Dollar"
    if denom in ("$1", "1$", "$"):
        if 1840 <= year <= 1873:
            return "Seated Liberty Dollar"
        if 1878 <= year <= 1921:
            return "Morgan Dollar"
        if 1921 <= year <= 1935:
            return "Peace Dollar"
        if 1971 <= year <= 1978:
            return "Eisenhower Dollar"
        if 1979 <= year <= 1999:
            return "Susan B. Anthony Dollar"
        if year >= 2000:
            return "Sacagawea Dollar"
    return ""


class Location(models.Model):
    name = models.CharField(max_length=120, unique=True)
    def __str__(self): return self.name

class InventoryItem(models.Model):
    internal_id = models.CharField(
        "Internal ID",
        max_length=20,
        unique=True,
        blank=True,
        help_text="Leave blank to generate automatically.",
    )
    created_at = models.DateTimeField(default=timezone.now)

    # Core description
    denomination = models.CharField(max_length=60, blank=True)     # e.g., "$1", "10C"
    series = models.CharField(max_length=120, blank=True)         # e.g., "Morgan Dollar"
    date_mm = models.CharField("Date / Mint Mark", max_length=20, blank=True)  # e.g., "1881-S"
    variety = models.CharField(max_length=120, blank=True)        # flexible (VAM/Overton/etc.)
    notes = models.TextField(blank=True)

    # Holder / grade
    holder = models.CharField("Grading Company", max_length=20, blank=True)  # PCGS/NGC/CACG/RAW
    grade_text = models.CharField("Grade", max_length=40, blank=True)  # "MS65", "RAW est AU+", "Details Cleaned"
    cert_number = models.CharField("Cert Number", max_length=40, blank=True)

    cac_sticker = models.BooleanField("CAC Sticker", default=False)
    cacg_holder = models.BooleanField(default=False)

    ask_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # Workflow
    STATUS_CHOICES = [
        ("IN_STOCK", "In Stock"),
        ("AT_GRADING", "At Grading"),
        ("LISTED", "Listed"),
        ("SOLD", "Sold"),
        ("REJECT_BULK", "Reject/Bulk"),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="IN_STOCK")

    location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="items")
    show_location = models.CharField(max_length=80, blank=True)  # e.g., "FUN 2026 case 2"

    # Crackout/dealer economics
    cost_basis = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    source = models.CharField(max_length=120, blank=True)
    acquired_date = models.DateField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.internal_id:
            self.internal_id = _next_code("ID", InventoryItem, "internal_id")
        super().save(*args, **kwargs)

    def __str__(self): return self.internal_id


class IncomingInventoryBatch(models.Model):
    STATUS_CHOICES = [
        ("UPLOADED", "Uploaded"),
        ("PARSED", "Parsed"),
        ("REVIEWED", "Reviewed"),
        ("IMPORTED", "Imported"),
        ("NEEDS_REVIEW", "Needs Review"),
    ]

    created_at = models.DateTimeField(default=timezone.now)
    title = models.CharField(max_length=160, blank=True)
    vendor = models.CharField(max_length=160, blank=True)
    invoice_number = models.CharField(max_length=80, blank=True)
    source_file = models.FileField(upload_to="incoming_invoices/", blank=True)
    source_text = models.TextField(blank=True)
    parser_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="UPLOADED")
    parser_notes = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "Incoming inventory batch"
        verbose_name_plural = "Incoming inventory batches"

    def __str__(self):
        return self.title or self.invoice_number or f"Incoming batch {self.pk}"


class IncomingInventoryLine(models.Model):
    batch = models.ForeignKey(IncomingInventoryBatch, on_delete=models.CASCADE, related_name="lines")
    created_at = models.DateTimeField(default=timezone.now)
    raw_description = models.TextField(blank=True)
    date_mm = models.CharField(max_length=20, blank=True)
    denomination = models.CharField(max_length=60, blank=True)
    series = models.CharField(max_length=120, blank=True)
    variety = models.CharField(max_length=120, blank=True)
    holder = models.CharField(max_length=20, blank=True)
    grade_text = models.CharField(max_length=40, blank=True)
    cert_number = models.CharField(max_length=40, blank=True)
    ask_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    cost_basis = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    source = models.CharField(max_length=120, blank=True)
    confidence = models.PositiveSmallIntegerField(default=0)
    needs_review = models.BooleanField(default=True)
    imported_item = models.ForeignKey(
        InventoryItem,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="incoming_lines",
    )

    class Meta:
        ordering = ("id",)

    def __str__(self):
        return self.raw_description[:80] or f"Incoming line {self.pk}"

class ItemPhoto(models.Model):
    item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name="photos")
    image = models.ImageField(upload_to="item_photos/")
    tag = models.CharField(max_length=40, blank=True)  # obv/rev/slab/other
    created_at = models.DateTimeField(default=timezone.now)

class Certification(models.Model):
    item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name="cert_history")
    service = models.CharField(max_length=20)          # PCGS/NGC/CACG/etc.
    grade_text = models.CharField(max_length=40, blank=True)
    cert_number = models.CharField(max_length=40, blank=True)
    captured_at = models.DateTimeField(default=timezone.now)

class Submission(models.Model):
    SERVICE_CHOICES = [("PCGS", "PCGS"), ("NGC", "NGC"), ("CAC", "CAC"), ("CACG", "CACG")]

    METHOD_CHOICES = [
        ("SHIPPED", "Shipped"),
        ("SHOW_DROPOFF", "Show Drop-Off"),
    ]

    CARRIER_CHOICES = [
        ("USPS", "USPS"),
        ("FEDEX", "FedEx"),
        ("UPS", "UPS"),
        ("OTHER", "Other"),
    ]

    internal_id = models.CharField(max_length=20, unique=True, blank=True)  # SUB-000001
    service = models.CharField(max_length=10, choices=SERVICE_CHOICES)
    created_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=30, default="PREPARED")
    notes = models.TextField(blank=True)

    grading_submission_number = models.CharField(max_length=50, blank=True)
    submission_method = models.CharField(max_length=20, choices=METHOD_CHOICES, default="SHIPPED")
    carrier = models.CharField(max_length=20, choices=CARRIER_CHOICES, blank=True)
    tracking_number = models.CharField(max_length=100, blank=True)
    show_name = models.CharField(max_length=120, blank=True)

    def save(self, *args, **kwargs):
        if not self.internal_id:
            self.internal_id = _next_code("SUB", Submission, "internal_id")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.internal_id

class SubmissionItem(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="submission_lines")
    created_at = models.DateTimeField(default=timezone.now)
    declared_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

class CrackoutEvent(models.Model):
    item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name="crackouts")
    from_service = models.CharField(max_length=20, blank=True)
    from_grade = models.CharField(max_length=40, blank=True)
    from_cert = models.CharField(max_length=40, blank=True)
    to_submission = models.ForeignKey(Submission, null=True, blank=True, on_delete=models.SET_NULL, related_name="crackout_events")
    reason = models.CharField(max_length=200, blank=True)
    outcome = models.CharField(max_length=200, blank=True)  # e.g., "Upgraded", "No grade", "Details"
    created_at = models.DateTimeField(default=timezone.now)

class Sale(models.Model):
    internal_id = models.CharField(max_length=20, unique=True, blank=True)  # SALE-000001
    created_at = models.DateTimeField(default=timezone.now)
    venue = models.CharField(max_length=80, blank=True)  # show/wholesale/IG/etc.
    notes = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if not self.internal_id:
            self.internal_id = _next_code("SALE", Sale, "internal_id")
        super().save(*args, **kwargs)

    def __str__(self): return self.internal_id

class SaleItem(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="sale_lines")
    sold_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

class Container(models.Model):
    internal_id = models.CharField(
        "Internal ID",
        max_length=20,
        unique=True,
        blank=True,
        help_text="Leave blank to generate automatically.",
    )
    created_at = models.DateTimeField(default=timezone.now)
    date_mm = models.CharField("Date / Mint Mark", max_length=20, blank=True)
    denomination = models.CharField(max_length=60, blank=True)
    series = models.CharField(max_length=120, blank=True)
    label_text = models.CharField(max_length=200, blank=True)  # "NGC rejects | Ike $1 MS | Qty 20"
    quantity = models.PositiveIntegerField(default=1)
    cost_basis = models.DecimalField("Cost", max_digits=12, decimal_places=2, null=True, blank=True)
    ask_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True)

    def generated_label_text(self):
        parts = [self.date_mm, self.denomination, self.series]
        label = " ".join(part for part in parts if part).strip()
        if self.quantity:
            label = f"{label} QTY {self.quantity}".strip()
        return label

    def display_label_text(self):
        return self.label_text or self.generated_label_text()

    def save(self, *args, **kwargs):
        if not self.internal_id:
            self.internal_id = _next_code("TUBE", Container, "internal_id")
        if not self.series:
            self.series = series_for_coin(self.date_mm, self.denomination)
        if not self.label_text:
            self.label_text = self.generated_label_text()
        super().save(*args, **kwargs)

    def __str__(self): return self.internal_id

    class Meta:
        verbose_name = "Tube"
        verbose_name_plural = "Tubes"

class SaleTube(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="tube_lines")
    tube = models.ForeignKey(Container, on_delete=models.PROTECT, related_name="sale_lines")
    sold_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)


class Product(models.Model):
    internal_id = models.CharField(
        "Product ID",
        max_length=20,
        unique=True,
        blank=True,
        help_text="Leave blank to generate automatically.",
    )
    name = models.CharField(max_length=160)
    sku = models.CharField("SKU", max_length=80, blank=True)
    quantity = models.PositiveIntegerField(default=0)
    cost_basis = models.DecimalField("Cost", max_digits=12, decimal_places=2, null=True, blank=True)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="products")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "internal_id")
        verbose_name = "Product"
        verbose_name_plural = "Products"

    def save(self, *args, **kwargs):
        if not self.internal_id:
            self.internal_id = _next_code("PROD", Product, "internal_id")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name or self.internal_id


class Report(models.Model):
    class Meta:
        managed = False
        verbose_name = "Report"
        verbose_name_plural = "Reports"

    def __str__(self):
        return "Reports"


class PricingPlan(models.Model):
    BILLING_INTERVAL_CHOICES = [
        ("MONTH", "Monthly"),
        ("YEAR", "12-month"),
        ("ONE_TIME", "One-time"),
        ("CUSTOM", "Custom"),
    ]

    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=90, unique=True)
    tagline = models.CharField(max_length=160, blank=True)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    billing_interval = models.CharField(max_length=10, choices=BILLING_INTERVAL_CHOICES, default="MONTH")
    trial_days = models.PositiveIntegerField(default=0)
    stripe_product_id = models.CharField(max_length=120, blank=True)
    stripe_price_id = models.CharField(max_length=120, blank=True)
    cta_label = models.CharField(max_length=80, default="Choose plan")
    cta_url = models.URLField(blank=True)
    feature_bullets = models.TextField(blank=True, help_text="One feature per line.")
    is_active = models.BooleanField(default=True)
    is_public = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    display_order = models.PositiveIntegerField(default=100)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("display_order", "price", "name")

    def __str__(self):
        return self.name

    @property
    def features(self):
        return [line.strip() for line in self.feature_bullets.splitlines() if line.strip()]


class ContactLead(models.Model):
    legal_business_name = models.CharField(max_length=180)
    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80)
    phone = models.CharField(max_length=40)
    email = models.EmailField()
    selected_plan = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.legal_business_name} - {self.first_name} {self.last_name}"
