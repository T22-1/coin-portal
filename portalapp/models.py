from __future__ import annotations
from django.db import models
from django.utils import timezone

def _next_code(prefix: str, model_cls: type[models.Model], field_name: str = "internal_id") -> str:
    # Generates sequential IDs like INV-000001. Good enough for MVP.
    last = model_cls.objects.order_by(f"-{field_name}").first()
    if not last:
        n = 1950
    else:
        s = getattr(last, field_name) or ""
        try:
            n = int(s.split("-")[-1]) + 1
        except Exception:
            n = 1
    return f"{prefix}-{n}"

class Location(models.Model):
    name = models.CharField(max_length=120, unique=True)
    def __str__(self): return self.name

class InventoryItem(models.Model):
    internal_id = models.CharField(max_length=20, unique=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    # Core description
    denomination = models.CharField(max_length=60, blank=True)     # e.g., "$1", "10C"
    series = models.CharField(max_length=120, blank=True)         # e.g., "Morgan Dollar"
    date_mm = models.CharField(max_length=20, blank=True)         # e.g., "1881-S"
    variety = models.CharField(max_length=120, blank=True)        # flexible (VAM/Overton/etc.)
    notes = models.TextField(blank=True)

    # Holder / grade
    holder = models.CharField(max_length=20, blank=True)          # PCGS/NGC/CACG/RAW
    grade_text = models.CharField(max_length=40, blank=True)      # "MS65", "RAW est AU+", "Details Cleaned"
    cert_number = models.CharField(max_length=40, blank=True)

    cac_sticker = models.BooleanField(default=False)
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
    internal_id = models.CharField(max_length=20, unique=True, blank=True)  # TUBE-000001
    created_at = models.DateTimeField(default=timezone.now)
    label_text = models.CharField(max_length=200, blank=True)  # "NGC rejects | Ike $1 MS | Qty 20"
    quantity = models.PositiveIntegerField(default=0)
    ask_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if not self.internal_id:
            self.internal_id = _next_code("TUBE", Container, "internal_id")
        super().save(*args, **kwargs)

    def __str__(self): return self.internal_id
