"""Transactional review and receiving workflow for incoming purchases."""
import hashlib
import logging
from decimal import Decimal
from pathlib import Path

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.forms import modelformset_factory
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.clickjacking import xframe_options_sameorigin

from .intake_parser import EXTENSIONS, read_purchase
from .models import IncomingInventoryBatch as Batch, IncomingInventoryLine as Line
from .models import InventoryItem, Container, Product, series_for_coin

logger = logging.getLogger(__name__)


def order_key(vendor, number):
    return f"{vendor.strip().casefold()}|{number.strip().casefold()}" if vendor.strip() and number.strip() else None


class PurchaseForm(forms.ModelForm):
    class Meta:
        model = Batch
        fields = ["title", "vendor", "invoice_number", "purchase_date", "shipping", "tax", "review_notes"]
        labels = {"vendor": "Seller", "invoice_number": "Order / Invoice Number", "review_notes": "Purchase Notes"}
        widgets = {"purchase_date": forms.DateInput(attrs={"type": "date"}), "review_notes": forms.Textarea(attrs={"rows": 2})}

    def clean(self):
        data = super().clean()
        for field in ("shipping", "tax"):
            if data.get(field) is not None and data[field] < 0:
                self.add_error(field, "Enter zero or a positive amount.")
        key = order_key(data.get("vendor", ""), data.get("invoice_number", ""))
        duplicate = Batch.objects.filter(Q(order_key=key) | Q(vendor__iexact=data.get("vendor", "").strip(),
                                                             invoice_number__iexact=data.get("invoice_number", "").strip()))
        if key and duplicate.exclude(pk=self.instance.pk).exists():
            raise ValidationError("This seller and order number already exist in Incoming Inventory.")
        self.instance.order_key = key
        return data


class LineForm(forms.ModelForm):
    selected = forms.BooleanField(required=False, label="Approve this row")

    class Meta:
        model = Line
        fields = ["destination", "raw_description", "date_mm", "denomination", "series", "holder", "grade_text",
                  "cert_number", "quantity", "coins_per_roll", "received_quantity", "cost_basis", "ask_price",
                  "checked", "excluded", "review_notes"]
        labels = {"raw_description": "Description", "date_mm": "Date / Mint Mark", "holder": "Grading Company",
                  "grade_text": "Grade", "cert_number": "Cert Number", "quantity": "Units / Rolls Ordered",
                  "cost_basis": "Total Merchandise Cost (Entire Row)", "checked": "Inspected and approved",
                  "excluded": "Exclude from inventory", "ask_price": "Ask Price Per Unit"}
        widgets = {"raw_description": forms.Textarea(attrs={"rows": 2}), "review_notes": forms.Textarea(attrs={"rows": 2})}

    def clean(self):
        data = super().clean()
        quantity = data.get("quantity", 0)
        if not 1 <= quantity <= 10000:
            self.add_error("quantity", "Enter between 1 and 10,000 units.")
        if data.get("received_quantity", 0) > quantity:
            self.add_error("received_quantity", "Received quantity exceeds the ordered quantity.")
        for field in ("cost_basis", "ask_price"):
            if data.get(field) is not None and data[field] < 0:
                self.add_error(field, "Enter zero or a positive amount.")
        if data.get("coins_per_roll") == 0:
            self.add_error("coins_per_roll", "Enter a positive count or leave blank.")
        if not data.get("series"):
            data["series"] = series_for_coin(data.get("date_mm", ""), data.get("denomination", ""))
        return data


LineSet = modelformset_factory(Line, form=LineForm, extra=0, max_num=500, validate_max=True)


@login_required
@permission_required("portalapp.view_incominginventorybatch", raise_exception=True)
def incoming_inventory(request):
    stage = request.GET.get("stage", "REVIEW")
    batches = Batch.objects.annotate(line_count=Count("lines")).order_by("-created_at")
    counts = dict(Batch.objects.values_list("stage").annotate(total=Count("id")))
    if stage in dict(Batch.STAGES):
        batches = batches.filter(stage=stage)
    page = Paginator(batches, 25).get_page(request.GET.get("page"))
    return render(request, "incoming_inventory.html", {"batches": page, "page": page, "stage": stage,
                  "stages": [(key, label, counts.get(key, 0)) for key, label in Batch.STAGES]})


@login_required
@permission_required("portalapp.add_incominginventorybatch", raise_exception=True)
@require_POST
def incoming_inventory_upload(request):
    file = request.FILES.get("invoice")
    if not file or Path(file.name).suffix.lower() not in EXTENSIONS or file.size > 20 * 1024 * 1024:
        messages.error(request, "Choose a PDF, image, CSV, TSV, TXT, or XLSX file up to 20 MB.")
        return redirect("incoming_inventory")
    digest = hashlib.sha256()
    for chunk in file.chunks():
        digest.update(chunk)
    file.seek(0)
    fingerprint = digest.hexdigest()
    existing = Batch.objects.filter(file_hash=fingerprint).first()
    if existing:
        messages.warning(request, "This document was already uploaded. Opening the existing purchase.")
        return redirect("incoming_inventory_batch", batch_id=existing.pk)
    try:
        with transaction.atomic():
            batch = Batch.objects.create(title=(request.POST.get("title") or file.name)[:160],
                                         vendor=request.POST.get("vendor", "")[:160],
                                         invoice_number=request.POST.get("invoice_number", "")[:80],
                                         file_hash=fingerprint, parser_status="NEEDS_REVIEW")
    except IntegrityError:
        return redirect("incoming_inventory_batch", batch_id=Batch.objects.get(file_hash=fingerprint).pk)
    stored = False
    try:
        batch.source_file.save(Path(file.name).name, file)
        stored = True
        text, rows, metadata, note = read_purchase(batch.source_file)
        key = order_key(batch.vendor or str(metadata.get("vendor") or ""), batch.invoice_number or str(metadata.get("invoice_number") or ""))
        duplicate = Batch.objects.filter(Q(order_key=key) | Q(
            vendor__iexact=(batch.vendor or str(metadata.get("vendor") or "")).strip(),
            invoice_number__iexact=(batch.invoice_number or str(metadata.get("invoice_number") or "")).strip()
        )).exclude(pk=batch.pk).first() if key else None
        if duplicate:
            batch.source_file.delete(save=False)
            batch.delete()
            messages.warning(request, "This seller/order is already in Incoming Inventory. Opening that purchase.")
            return redirect("incoming_inventory_batch", batch_id=duplicate.pk)
        # Reuse form validation for untrusted extracted metadata.
        form = PurchaseForm({"title": batch.title, "vendor": batch.vendor or str(metadata.get("vendor") or "")[:160],
            "invoice_number": batch.invoice_number or str(metadata.get("invoice_number") or "")[:80],
            "purchase_date": metadata.get("purchase_date") or "", "shipping": metadata.get("shipping") or 0,
            "tax": metadata.get("tax") or 0, "review_notes": ""}, instance=batch)
        if form.is_valid():
            form.save()
        else:
            batch.refresh_from_db()
            note += " Metadata needs review: " + form.errors.as_text()
        with transaction.atomic():
            for row in rows:
                for field in ("cost_basis", "ask_price"):
                    value = row.get(field)
                    if value is not None and (not value.is_finite() or value < 0 or value >= Decimal("10000000000")):
                        row[field] = None
                Line.objects.create(batch=batch, **row)
            batch.source_text = text
            batch.parser_notes = note
            batch.save(update_fields=["source_text", "parser_notes"])
        messages.success(request, f"Found {len(rows)} draft row(s). Nothing has been added to inventory yet.")
    except Exception:
        logger.exception("Purchase extraction failed for batch %s", batch.pk)
        batch.parser_notes = ("Extraction could not finish. Original document retained; add rows manually for review." if stored
                              else "The document could not be stored. Check upload storage before uploading again.")
        if not stored:
            batch.file_hash = None
            batch.source_file = ""
            batch.save(update_fields=["file_hash", "source_file"])
        batch.save(update_fields=["parser_notes"])
        messages.warning(request, batch.parser_notes)
    return redirect("incoming_inventory_batch", batch_id=batch.pk)


def cost_allocations(lines, shipping, tax):
    active = [line for line in lines if not line.excluded]
    if not active or any(line.cost_basis is None for line in active):
        raise ValidationError("Enter merchandise costs for every included row before approval (zero is allowed).")
    total = sum(line.cost_basis for line in active)
    weights = [line.cost_basis if total else Decimal(line.quantity) for line in active]
    denominator = sum(weights)
    extra = shipping + tax
    remaining = extra
    result = {}
    for index, (line, weight) in enumerate(zip(active, weights)):
        share = remaining if index == len(active) - 1 else (extra * weight / denominator).quantize(Decimal("0.01"))
        # Rounding cannot allocate more than the remaining invoice charges.
        share = min(share, remaining)
        remaining -= share
        result[line.pk] = line.cost_basis + share
    return result


def allocate_costs(lines, shipping, tax):
    amounts = cost_allocations(lines, shipping, tax)
    for line in lines:
        if line.pk not in amounts:
            continue
        line.allocated_cost = amounts[line.pk]
        line.save(update_fields=["allocated_cost"])


def approve(batch, selected, user):
    """Caller holds a batch row lock; inventory and markers commit together."""
    lines = list(batch.lines.all())
    candidates = [line for line in lines if str(line.pk) in selected and not line.imported_at and not line.imported_item_id]
    if not candidates:
        raise ValidationError("Select at least one unimported row.")
    if batch.stage != "QC":
        raise ValidationError("Move this purchase to Quality Check after it arrives, then approve received rows.")
    for line in candidates:
        model = {"TUBES": "container", "PRODUCTS": "product"}.get(line.destination, "inventoryitem")
        if not user.has_perm(f"portalapp.add_{model}"):
            raise ValidationError(f"You do not have permission to add {line.get_destination_display()}.")
        if line.excluded or not line.checked or line.received_quantity != line.quantity:
            raise ValidationError(f"Row {line.pk}: confirm inspection and full received quantity, or split the row first.")
        if line.destination != "PRODUCTS" and not (line.date_mm and line.denomination and line.series):
            raise ValidationError(f"Row {line.pk}: date, denomination, and series are required.")
        if line.destination == "NUMISMATIC" and line.holder.upper() == "RAW":
            raise ValidationError(f"Row {line.pk}: choose Raw or correct the grading company.")
        if line.destination == "PRODUCTS" and not line.raw_description:
            raise ValidationError(f"Row {line.pk}: a product description is required.")
        if line.cert_number and line.quantity != 1:
            raise ValidationError(f"Row {line.pk}: split certified coins into individual rows with their own certificates.")
        if line.quantity > 250 and line.destination != "PRODUCTS":
            raise ValidationError("Approve no more than 250 individual records per row; split large lots first.")
    if not any(line.imported_at or line.imported_item_id for line in lines):
        allocate_costs(lines, batch.shipping, batch.tax)
    for line in candidates:
        if line.allocated_cost is None:
            raise ValidationError("This row has no allocated cost. Review the purchase before approval.")
        notes = f"Incoming purchase {batch.pk}; {line.raw_description}\n{line.review_notes}"
        records = []
        unit = (line.allocated_cost / line.quantity).quantize(Decimal("0.01"), rounding="ROUND_DOWN")
        if line.destination == "PRODUCTS":
            # Split cost lots by a cent when necessary so quantity * unit cost reconciles exactly.
            pennies = int((line.allocated_cost - unit * line.quantity) * 100)
            for quantity, cost in ((line.quantity - pennies, unit), (pennies, unit + Decimal("0.01"))):
                if quantity:
                    obj = Product.objects.create(name=line.raw_description[:160], quantity=quantity,
                                                 cost_basis=cost, unit_price=line.ask_price, notes=notes)
                    records.append({"id": obj.internal_id, "url": reverse("admin:portalapp_product_change", args=[obj.pk])})
        else:
            for index in range(line.quantity):
                cost = line.allocated_cost - unit * (line.quantity - 1) if index == line.quantity - 1 else unit
                fields = dict(date_mm=line.date_mm, denomination=line.denomination, series=line.series,
                              cost_basis=cost, ask_price=line.ask_price, notes=notes)
                if line.destination == "TUBES":
                    obj = Container.objects.create(**fields, quantity=1,
                        label_text=" ".join(filter(None, [line.date_mm, line.denomination, line.series,
                            f"{line.coins_per_roll} coins" if line.coins_per_roll else "Roll"])))
                    model = "container"
                else:
                    obj = InventoryItem.objects.create(**fields, holder="RAW" if line.destination == "RAW" else line.holder,
                        grade_text=line.grade_text, cert_number=line.cert_number, variety=line.variety,
                        source=(batch.vendor or line.source)[:120], acquired_date=batch.purchase_date)
                    model = "inventoryitem"
                    if index == 0:
                        line.imported_item = obj
                records.append({"id": obj.internal_id, "url": reverse(f"admin:portalapp_{model}_change", args=[obj.pk])})
        line.imported_records = records
        line.imported_at = timezone.now()
        line.needs_review = False
        line.save(update_fields=["imported_records", "imported_at", "imported_item", "needs_review"])
    return len(candidates)


@login_required
@permission_required("portalapp.change_incominginventorybatch", raise_exception=True)
def incoming_inventory_batch(request, batch_id):
    with transaction.atomic():
        batch = get_object_or_404(Batch.objects.select_for_update(), pk=batch_id)
        frozen = batch.lines.filter(Q(imported_at__isnull=False) | Q(imported_item__isnull=False)).exists()
        pending = batch.lines.filter(imported_at__isnull=True, imported_item__isnull=True)
        data = request.POST if request.method == "POST" else None
        form = PurchaseForm(data, instance=batch)
        rows = LineSet(data, queryset=pending, prefix="lines")
        if frozen:
            for name in ("shipping", "tax"):
                form.fields[name].disabled = True
            for row_form in rows:
                for name in ("cost_basis", "quantity", "excluded"):
                    row_form.fields[name].disabled = True
        action = request.POST.get("action")
        if request.method == "POST":
            if batch.stage == "ARCHIVED":
                if action == "reopen":
                    batch.stage = "QC"
                    batch.archived_at = None
                    batch.save(update_fields=["stage", "archived_at"])
                    return redirect("incoming_inventory_batch", batch_id=batch.pk)
                messages.error(request, "Reopen this archived purchase before making changes.")
            elif form.is_valid() and rows.is_valid():
                try:
                    with transaction.atomic():
                        expected = set(pending.values_list("pk", flat=True))
                        submitted = [row.cleaned_data.get("id") for row in rows]
                        if len(submitted) != len(expected) or {obj.pk for obj in submitted if obj} != expected:
                            raise ValidationError("Rows changed since this page loaded. Reload the purchase and try again.")
                        form.save()
                        rows.save()
                        if action == "add":
                            if frozen:
                                raise ValidationError("All purchase rows must be added before the first approval.")
                            Line.objects.create(batch=batch)
                        elif action and action.startswith("split:"):
                            if frozen:
                                raise ValidationError("Split rows before the first approval so costs stay consistent.")
                            original = get_object_or_404(pending, pk=action.split(":")[1])
                            if original.quantity < 2:
                                raise ValidationError("A row must contain at least two units to split.")
                            cost = (original.cost_basis / original.quantity).quantize(Decimal("0.01")) if original.cost_basis is not None else None
                            original.quantity -= 1
                            if cost is not None:
                                original.cost_basis -= cost
                            original.received_quantity = min(original.received_quantity, original.quantity)
                            original.checked = False
                            original.save()
                            original.pk = None
                            original.quantity = 1
                            original.received_quantity = 0
                            original.cost_basis = cost
                            original.save()
                        elif action in {"awaiting", "receive", "review"}:
                            batch.stage = {"awaiting": "AWAITING", "receive": "QC", "review": "REVIEW"}[action]
                            batch.save(update_fields=["stage"])
                        elif action == "import":
                            selected = {str(row.instance.pk) for row in rows if row.cleaned_data.get("selected")}
                            count = approve(batch, selected, request.user)
                            messages.success(request, f"Added {count} row(s) to inventory.")
                        elif action == "archive":
                            if pending.filter(excluded=False).exists():
                                raise ValidationError("Approve or exclude every remaining row before archiving.")
                            batch.stage = "ARCHIVED"
                            batch.archived_at = timezone.now()
                            batch.save(update_fields=["stage", "archived_at"])
                    messages.success(request, "Purchase saved.")
                    return redirect("incoming_inventory_batch", batch_id=batch.pk)
                except (ValidationError, IntegrityError) as exc:
                    if isinstance(exc, IntegrityError):
                        logger.exception("Purchase approval/save conflict for batch %s", batch.pk)
                    messages.error(request, "; ".join(exc.messages) if isinstance(exc, ValidationError) else "A duplicate order or inventory ID conflict prevented saving. No changes were saved; reload and try again.")
        completed = batch.lines.filter(Q(imported_at__isnull=False) | Q(imported_item__isnull=False))
        merchandise = sum((line.cost_basis or Decimal(0)) for line in batch.lines.filter(excluded=False))
        try:
            estimates = cost_allocations(list(batch.lines.all()), batch.shipping, batch.tax)
        except ValidationError:
            estimates = {}
        for row in rows:
            row.instance.preview_cost = row.instance.allocated_cost if frozen else estimates.get(row.instance.pk)
        destinations = set(pending.filter(excluded=False).values_list("destination", flat=True))
        suffix = Path(batch.source_file.name).suffix.lower()
        return render(request, "incoming_inventory_batch.html", {"batch": batch, "form": form, "rows": rows,
                      "completed": completed, "frozen": frozen, "merchandise_total": merchandise,
                      "purchase_total": merchandise + batch.shipping + batch.tax,
                      "missing_costs": batch.lines.filter(excluded=False, cost_basis__isnull=True).exists(),
                      "approval_label": "Approve & Add to Tubes" if destinations == {"TUBES"} else "Approve & Add to Inventory",
                      "source_pdf": suffix == ".pdf", "source_image": suffix in {".png", ".jpg", ".jpeg", ".webp"}})


@login_required
@permission_required("portalapp.view_incominginventorybatch", raise_exception=True)
@xframe_options_sameorigin
def incoming_document(request, batch_id):
    batch = get_object_or_404(Batch, pk=batch_id)
    if not batch.source_file:
        from django.http import Http404
        raise Http404
    inline = request.GET.get("preview") == "1" and Path(batch.source_file.name).suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
    response = FileResponse(batch.source_file.open("rb"), as_attachment=not inline, filename=Path(batch.source_file.name).name)
    response["X-Content-Type-Options"] = "nosniff"
    return response
