from __future__ import annotations
import csv
import hashlib
from decimal import Decimal, InvalidOperation
from io import BytesIO
from io import StringIO
from pathlib import Path
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from reportlab.lib.units import inch
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode import code128
from pypdf import PdfReader, PdfWriter
from pypdf.generic import BooleanObject, NameObject, TextStringObject

from .models import InventoryItem, Container, Sale, SaleItem, Submission, SubmissionItem, PricingPlan


ITEM_PREFIXES = ("ID-", "INV-")
SELLABLE_STATUSES = {"IN_STOCK", "LISTED"}
ACTIVE_SUBMISSION_STATUSES = {"PREPARED", "SUBMITTED", "SHIPPED", "AT_GRADING"}
CAC_ALLOWED_HOLDERS = {"PCGS", "NGC"}

def login_view(request: HttpRequest):
    if request.method == "POST":
        username = request.POST.get("username","")
        password = request.POST.get("password","")
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            return redirect("home")
        return render(request, "login.html", {"error":"Invalid username/password"})
    return render(request, "login.html")

def logout_view(request: HttpRequest):
    logout(request)
    return redirect("login")

@login_required
def home(request: HttpRequest):
    return render(request, "home.html")


@login_required
def pricing(request: HttpRequest):
    plans = PricingPlan.objects.filter(is_active=True, is_public=True).order_by("display_order", "price", "name")
    return render(request, "pricing.html", {"plans": plans})

@login_required
def scan(request: HttpRequest):
    # Simple scan box: scan code -> redirect to record
    if request.method == "POST":
        code = (request.POST.get("code") or "").strip()
        if code.upper().startswith("TUBE-"):
            return redirect("tube_by_code", code=code.upper())
        if code.upper().startswith(ITEM_PREFIXES):
            return redirect("item_by_code", code=code.upper())
        # Allow scanning raw numeric and treating it as internal id
        return render(
            request,
            "scan.html",
            {"error": "Code not recognized. Use ID-###### or TUBE-######."},
        )
    return render(request, "scan.html")

@login_required
def item_by_code(request: HttpRequest, code: str):
    item = get_object_or_404(InventoryItem, internal_id=code.upper())
    return render(request, "item.html", {"item": item})

@login_required
def tube_by_code(request: HttpRequest, code: str):
    tube = get_object_or_404(Container, internal_id=code.upper())
    return render(request, "tube.html", {"tube": tube})

@login_required
def sale_start(request: HttpRequest):
    # starts a sale batch in session
    request.session["sale_batch"] = []
    request.session.modified = True
    return redirect("sale_batch")

@login_required
@require_http_methods(["POST"])
def sale_add_scan(request: HttpRequest):
    code = (request.POST.get("code") or "").strip().upper()
    batch = request.session.get("sale_batch", [])
    if not isinstance(batch, list):
        batch = []
    if code.startswith(ITEM_PREFIXES):
        try:
            item = InventoryItem.objects.get(internal_id=code)
        except InventoryItem.DoesNotExist:
            messages.warning(request, f"{code} was not found.")
        else:
            if item.status not in SELLABLE_STATUSES:
                messages.warning(request, f"{code} is {item.get_status_display()} and cannot be added to a sale.")
            elif code not in batch:
                batch.append(code)
    elif code.startswith("TUBE-"):
        if code not in batch:
            batch.append(code)
    request.session["sale_batch"] = batch
    request.session.modified = True
    return redirect("sale_batch")

@login_required
def sale_batch(request: HttpRequest):
    batch = request.session.get("sale_batch", [])
    items = []
    tubes = []
    for code in batch:
        if code.startswith(ITEM_PREFIXES):
            try:
                items.append(InventoryItem.objects.get(internal_id=code))
            except InventoryItem.DoesNotExist:
                pass
        elif code.startswith("TUBE-"):
            try:
                tubes.append(Container.objects.get(internal_id=code))
            except Container.DoesNotExist:
                pass
    return render(request, "sale_batch.html", {"items": items, "tubes": tubes})

@login_required
@require_http_methods(["POST"])
def sale_complete(request: HttpRequest):
    venue = (request.POST.get("venue") or "").strip()
    sale = Sale.objects.create(venue=venue)

    # Items
    batch = request.session.get("sale_batch", [])
    for code in batch:
        if code.startswith(ITEM_PREFIXES):
            try:
                item = InventoryItem.objects.get(internal_id=code)
            except InventoryItem.DoesNotExist:
                continue
            if item.status not in SELLABLE_STATUSES:
                messages.warning(request, f"{code} is {item.get_status_display()} and was not sold.")
                continue
            key = f"price_{code}"
            price_raw = (request.POST.get(key) or "").strip().replace(",","")
            sold_price = None
            if price_raw:
                try:
                    sold_price = Decimal(price_raw)
                except InvalidOperation:
                    sold_price = None
            SaleItem.objects.create(sale=sale, item=item, sold_price=sold_price)
            item.status = "SOLD"
            item.save(update_fields=["status"])
        elif code.startswith("TUBE-"):
            # For tubes we just clear it out (MVP). Later we can log tube sales too.
            pass

    request.session["sale_batch"] = []
    request.session.modified = True
    return redirect("sale_batch")

def _label_pdf_response(buf: BytesIO, filename: str) -> HttpResponse:
    resp = HttpResponse(buf.getvalue(), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{filename}"'
    return resp


LABEL_WIDTH = 2 * inch
LABEL_HEIGHT = 0.75 * inch
LABEL_MARGIN_X = 0.07 * inch
LABEL_BARCODE_HEIGHT = 0.20 * inch
LABEL_BARCODE_Y = 0.05 * inch


def _draw_fit_text(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    max_width: float,
    font_name: str,
    max_size: float,
    min_size: float,
) -> None:
    size = max_size
    while size > min_size and c.stringWidth(text, font_name, size) > max_width:
        size -= 0.25
    c.setFont(font_name, size)
    c.drawString(x, y, text)


def _fit_code128(
    value: str,
    max_width: float,
    max_bar_width: float,
    min_bar_width: float,
    bar_height: float = LABEL_BARCODE_HEIGHT,
):
    bar_width = max_bar_width
    while bar_width > min_bar_width:
        barcode = code128.Code128(
            value,
            barHeight=bar_height,
            barWidth=bar_width,
            humanReadable=False,
        )
        if barcode.width <= max_width:
            return barcode
        bar_width -= 0.0004 * inch
    return code128.Code128(
        value,
        barHeight=bar_height,
        barWidth=min_bar_width,
        humanReadable=False,
    )

@login_required
def label_item_pdf(request: HttpRequest, code: str):

    item = get_object_or_404(InventoryItem, internal_id=code.upper())

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(LABEL_WIDTH, LABEL_HEIGHT))
    _draw_item_label(c, item)
    c.save()
    buf.seek(0)
    return _label_pdf_response(buf, f"{item.internal_id}.pdf")


def _draw_item_label(c: canvas.Canvas, item: InventoryItem) -> None:
    c.setPageSize((LABEL_WIDTH, LABEL_HEIGHT))

    x_margin = LABEL_MARGIN_X
    usable_width = LABEL_WIDTH - (2 * x_margin)
    y_top = 0.58 * inch

    # Line 1: internal id
    _draw_fit_text(c, item.internal_id, x_margin, y_top, usable_width, "Helvetica-Bold", 8.5, 6.0)

    # Line 2: details
    details = []
    if item.date_mm:
        details.append(item.date_mm)
    if item.denomination:
        details.append(item.denomination)
    if item.holder:
        details.append(item.holder)
    if item.grade_text:
        details.append(item.grade_text)
    if item.cacg_holder:
        details.append("CACG")
    elif item.cac_sticker:
        details.append("CAC")

    line2 = " | ".join(details)
    _draw_fit_text(c, line2, x_margin, y_top - 0.12 * inch, usable_width, "Helvetica", 5.5, 4.5)

    # Line 3: ask
    ask = f"ASK ${item.ask_price:,.2f}" if item.ask_price is not None else "ASK $"
    _draw_fit_text(c, ask, x_margin, y_top - 0.22 * inch, usable_width, "Helvetica-Bold", 6.5, 5.0)

    # Barcode
    barcode = _fit_code128(item.internal_id, usable_width, 0.0078 * inch, 0.0045 * inch)
    barcode.drawOn(c, x_margin + ((usable_width - barcode.width) / 2), LABEL_BARCODE_Y)

    c.showPage()


@login_required
def label_tube_pdf(request: HttpRequest, code: str):
    tube = get_object_or_404(Container, internal_id=code.upper())
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(LABEL_WIDTH, LABEL_HEIGHT))
    _draw_tube_label(c, tube)
    c.save()
    buf.seek(0)
    return _label_pdf_response(buf, f"{tube.internal_id}.pdf")


def _draw_tube_label(c: canvas.Canvas, tube: Container) -> None:
    c.setPageSize((LABEL_WIDTH, LABEL_HEIGHT))

    x_margin = LABEL_MARGIN_X
    usable_width = LABEL_WIDTH - (2 * x_margin)
    y_top = 0.58 * inch

    _draw_fit_text(c, tube.internal_id, x_margin, y_top, usable_width, "Helvetica-Bold", 10, 6.0)

    _draw_fit_text(c, tube.label_text or "", x_margin, y_top - 0.16 * inch, usable_width, "Helvetica", 7.0, 4.5)

    barcode = _fit_code128(tube.internal_id, usable_width, 0.010 * inch, 0.0045 * inch)
    barcode.drawOn(c, x_margin + ((usable_width - barcode.width) / 2), LABEL_BARCODE_Y)

    c.showPage()


def item_labels_pdf_response(items, filename: str = "inventory-labels.pdf") -> HttpResponse:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(LABEL_WIDTH, LABEL_HEIGHT))
    for item in items:
        _draw_item_label(c, item)
    c.save()
    buf.seek(0)
    return _label_pdf_response(buf, filename)


def _submission_lines(submission: Submission):
    return (
        SubmissionItem.objects.filter(submission=submission)
        .select_related("item")
        .order_by("created_at", "id")
    )


def _item_description(item: InventoryItem) -> str:
    parts = [item.date_mm, item.denomination, item.series, item.variety]
    return " ".join(part for part in parts if part).strip()


def _submission_export_rows(submission: Submission):
    rows = []
    for line in _submission_lines(submission):
        item = line.item
        rows.append(
            {
                "line_id": line.id,
                "portal_id": item.internal_id,
                "description": _item_description(item),
                "date_mm": item.date_mm,
                "denomination": item.denomination,
                "series": item.series,
                "variety": item.variety,
                "holder": item.holder,
                "grade": item.grade_text,
                "cert_number": item.cert_number,
                "declared_value": line.declared_value or item.cost_basis or item.ask_price or "",
                "notes": item.notes,
            }
        )
    return rows


def _submission_stable_queryset():
    return Submission.objects.only(
        "id",
        "internal_id",
        "service",
        "status",
        "created_at",
        "notes",
    )


def _pcgs_submission_number(submission: Submission) -> str:
    return _submission_form_number(submission, "PCGS")


def _submission_form_number(submission: Submission, service: str) -> str:
    seed = f"{service}:{submission.pk}:{submission.internal_id}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    return str((int(digest[:12], 16) % 9_000_000) + 1_000_000)


def _active_submission_lines_for_item(item: InventoryItem):
    return SubmissionItem.objects.filter(
        item=item,
        submission__status__in=ACTIVE_SUBMISSION_STATUSES,
    ).select_related("submission")


def _submission_rejection_reason(submission: Submission, item: InventoryItem) -> str:
    active_line = _active_submission_lines_for_item(item).exclude(submission=submission).first()
    if active_line:
        return f"{item.internal_id} is already on active submission {active_line.submission.internal_id}."

    if submission.service == "CAC" and item.holder.upper() not in CAC_ALLOWED_HOLDERS:
        return f"{item.internal_id} cannot be added to CAC unless it is already in a PCGS or NGC holder."

    return ""


@login_required
def submission_packet(request: HttpRequest, submission_id: int):
    submission = get_object_or_404(_submission_stable_queryset(), pk=submission_id)
    return render(
        request,
        "submission_packet.html",
        {
            "submission": submission,
            "lines": _submission_lines(submission),
            "rows": _submission_export_rows(submission),
        },
    )


@login_required
@require_http_methods(["POST"])
def submission_add_scan(request: HttpRequest, submission_id: int):
    submission = get_object_or_404(_submission_stable_queryset(), pk=submission_id)
    raw_codes = (request.POST.get("codes") or "").replace(",", "\n")
    codes = [code.strip().upper() for code in raw_codes.splitlines() if code.strip()]

    added = 0
    already_present = 0
    not_found = []
    rejected = []
    for code in codes:
        try:
            item = InventoryItem.objects.get(internal_id=code)
        except InventoryItem.DoesNotExist:
            not_found.append(code)
            continue

        rejection_reason = _submission_rejection_reason(submission, item)
        if rejection_reason:
            rejected.append(rejection_reason)
            continue

        _, created = SubmissionItem.objects.get_or_create(
            submission=submission,
            item=item,
            defaults={"declared_value": item.cost_basis or item.ask_price},
        )
        if created:
            item.status = "AT_GRADING"
            item.save(update_fields=["status"])
            added += 1
        else:
            already_present += 1

    if added:
        messages.success(request, f"Added {added} coin{'s' if added != 1 else ''} to {submission.internal_id}.")
    if already_present:
        messages.info(request, f"{already_present} coin{'s were' if already_present != 1 else ' was'} already in this submission.")
    if not_found:
        messages.warning(request, "Not found: " + ", ".join(not_found))
    for rejection in rejected:
        messages.warning(request, rejection)
    if not codes:
        messages.warning(request, "Scan or type at least one coin ID.")

    return redirect("submission_packet", submission_id=submission.id)


@login_required
@require_http_methods(["POST"])
def submission_remove_item(request: HttpRequest, submission_id: int, line_id: int):
    submission = get_object_or_404(_submission_stable_queryset(), pk=submission_id)
    line = get_object_or_404(SubmissionItem.objects.select_related("item"), pk=line_id, submission=submission)
    item = line.item
    item_code = item.internal_id
    line.delete()

    still_submitted = SubmissionItem.objects.filter(item=item).exists()
    if item.status == "AT_GRADING" and not still_submitted:
        item.status = "IN_STOCK"
        item.save(update_fields=["status"])

    messages.success(request, f"Removed {item_code} from {submission.internal_id}.")
    return redirect("submission_packet", submission_id=submission.id)


@login_required
def submission_packet_csv(request: HttpRequest, submission_id: int):
    submission = get_object_or_404(_submission_stable_queryset(), pk=submission_id)
    out = StringIO()
    fieldnames = [
        "portal_id",
        "description",
        "date_mm",
        "denomination",
        "series",
        "variety",
        "holder",
        "grade",
        "cert_number",
        "declared_value",
        "notes",
    ]
    writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in _submission_export_rows(submission):
        writer.writerow(row)

    response = HttpResponse(out.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{submission.internal_id}-packet.csv"'
    return response


@login_required
def submission_packet_pdf(request: HttpRequest, submission_id: int):
    submission = get_object_or_404(_submission_stable_queryset(), pk=submission_id)
    rows = _submission_export_rows(submission)

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    margin = 0.55 * inch
    y = height - margin

    def new_page():
        nonlocal y
        c.showPage()
        y = height - margin

    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin, y, f"Submission Packet: {submission.internal_id}")
    y -= 0.25 * inch
    c.setFont("Helvetica", 10)
    c.drawString(margin, y, f"Service: {submission.service}    Status: {submission.status}    Coins: {len(rows)}")
    y -= 0.35 * inch

    headers = ["ID", "Coin", "Holder", "Grade", "Cert", "Value"]
    x_positions = [margin, margin + 1.0 * inch, margin + 3.35 * inch, margin + 4.05 * inch, margin + 4.8 * inch, margin + 5.8 * inch]

    def draw_header():
        nonlocal y
        c.setFont("Helvetica-Bold", 8)
        for label, x in zip(headers, x_positions):
            c.drawString(x, y, label)
        y -= 0.16 * inch
        c.line(margin, y, width - margin, y)
        y -= 0.12 * inch

    draw_header()
    c.setFont("Helvetica", 7.5)
    for row in rows:
        if y < margin + 0.35 * inch:
            new_page()
            draw_header()
            c.setFont("Helvetica", 7.5)
        values = [
            row["portal_id"],
            row["description"][:42],
            row["holder"],
            row["grade"],
            row["cert_number"],
            str(row["declared_value"]),
        ]
        for value, x in zip(values, x_positions):
            c.drawString(x, y, str(value))
        y -= 0.2 * inch

    c.save()
    buf.seek(0)
    return _label_pdf_response(buf, f"{submission.internal_id}-packet.pdf")


def _pcgs_template_path() -> Path:
    return Path(__file__).resolve().parent / "pdf_templates" / "pcgs_show_submission.pdf"


def _submission_template_path(filename: str) -> Path:
    return Path(__file__).resolve().parent / "pdf_templates" / filename


def _format_declared_value(value) -> str:
    if value in ("", None):
        return ""
    try:
        return f"{Decimal(value):.2f}"
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


def _write_pdf_fields(writer: PdfWriter, field_values: dict[str, str]) -> None:
    if "/AcroForm" in writer._root_object:
        writer._root_object["/AcroForm"].update({NameObject("/NeedAppearances"): BooleanObject(True)})

    for page in writer.pages:
        writer.update_page_form_field_values(page, field_values)
        annotations = page.get("/Annots")
        if not annotations:
            continue
        for annotation_ref in annotations.get_object():
            annotation = annotation_ref.get_object()
            field_name = annotation.get("/T")
            if field_name in field_values:
                annotation[NameObject("/V")] = TextStringObject(str(field_values[field_name]))
                if "/AP" in annotation:
                    del annotation["/AP"]


def _draw_pdf_field_values(writer: PdfWriter, field_values: dict[str, str]) -> None:
    for page in writer.pages:
        annotations = page.get("/Annots")
        if not annotations:
            continue

        page_width = float(page.mediabox.width)
        page_height = float(page.mediabox.height)
        overlay_buffer = BytesIO()
        overlay = canvas.Canvas(overlay_buffer, pagesize=(page_width, page_height))
        overlay.setFillColorRGB(0, 0, 0)

        drew_text = False
        for annotation_ref in annotations.get_object():
            annotation = annotation_ref.get_object()
            field_name = annotation.get("/T")
            value = field_values.get(field_name)
            if value in ("", None):
                continue

            rect = annotation.get("/Rect")
            if not rect:
                continue
            x1, y1, x2, y2 = [float(v) for v in rect]
            field_height = max(y2 - y1, 1)
            font_size = min(8.0, max(5.0, field_height - 3.0))
            overlay.setFont("Helvetica", font_size)
            overlay.drawString(x1 + 1.5, y1 + max(1.5, (field_height - font_size) / 2), str(value)[:80])
            drew_text = True

        overlay.save()
        if drew_text:
            overlay_buffer.seek(0)
            page.merge_page(PdfReader(overlay_buffer).pages[0])


@login_required
def submission_pcgs_pdf(request: HttpRequest, submission_id: int):
    submission = get_object_or_404(_submission_stable_queryset(), pk=submission_id)
    rows = _submission_export_rows(submission)[:20]

    reader = PdfReader(str(_pcgs_template_path()))
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)

    field_values = {"SubmissionNumber": _pcgs_submission_number(submission)}
    total_declared_value = Decimal("0")
    for index, row in enumerate(rows, start=1):
        declared_value = row["declared_value"]
        if declared_value not in ("", None):
            try:
                total_declared_value += Decimal(declared_value)
            except (InvalidOperation, TypeError, ValueError):
                pass

        field_values.update(
            {
                f"QTY{index}": "1",
                f"COIN NUMBER{index}": "",
                f"DATEMINT MARK{index}": row["date_mm"],
                f"DENOM{index}": row["denomination"],
                f"COIN DESCRIPTIONVARIETY{index}": "",
                f"GRADEM_{index}": "",
                f"CERTIFICATION NUMBERM_{index}": "",
                f"DECLARED VALUE REQUIREDM_{index}": _format_declared_value(declared_value),
            }
        )

    field_values["DECLARED VALUE REQUIREDTOTAL DECLARED VALUE"] = _format_declared_value(total_declared_value)
    _write_pdf_fields(writer, field_values)
    _draw_pdf_field_values(writer, field_values)

    buf = BytesIO()
    writer.write(buf)
    buf.seek(0)
    response = HttpResponse(buf.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{submission.internal_id}-pcgs.pdf"'
    return response


@login_required
def submission_ngc_pdf(request: HttpRequest, submission_id: int):
    submission = get_object_or_404(_submission_stable_queryset(), pk=submission_id)
    rows = _submission_export_rows(submission)[:14]

    reader = PdfReader(str(_submission_template_path("ngc_submission.pdf")))
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)

    form_number = _submission_form_number(submission, "NGC")
    field_values = {
        "InvoiceNumber": form_number,
        "Invoice Number from NGC Submission": form_number,
        "TotalCoins": str(len(rows)),
    }
    total_declared_value = Decimal("0")
    for index, row in enumerate(rows, start=1):
        declared_value = row["declared_value"]
        if declared_value not in ("", None):
            try:
                total_declared_value += Decimal(declared_value)
            except (InvalidOperation, TypeError, ValueError):
                pass

        field_values.update(
            {
                f"Qty {index}": "1",
                f"Country {index}": "USA",
                f"Coin Date {index}": row["date_mm"],
                f"Denomination{index}": row["denomination"],
                f"Variety{index}": "",
                f"CrossOver Grade {index}": "",
                f"Certification{index}": "",
                f"Declare Value{index}": _format_declared_value(declared_value),
            }
        )

    field_values["TotalDeclaredValue"] = _format_declared_value(total_declared_value)
    _write_pdf_fields(writer, field_values)
    _draw_pdf_field_values(writer, field_values)

    buf = BytesIO()
    writer.write(buf)
    buf.seek(0)
    response = HttpResponse(buf.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{submission.internal_id}-ngc.pdf"'
    return response


def _fillable_submission_form_response(
    template_filename: str,
    submission: Submission,
    field_values: dict[str, str],
    service: str,
    draw_visible_values: bool = True,
) -> HttpResponse:
    reader = PdfReader(str(_submission_template_path(template_filename)))
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    _write_pdf_fields(writer, field_values)
    if draw_visible_values:
        _draw_pdf_field_values(writer, field_values)

    buf = BytesIO()
    writer.write(buf)
    buf.seek(0)
    response = HttpResponse(buf.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{submission.internal_id}-{service.lower()}.pdf"'
    return response


def _cac_field_values(submission: Submission, rows: list[dict]) -> dict[str, str]:
    field_values = {
        "shipment_this_submission_number": "1",
        "shipment_total_submissions": "1",
    }
    total_declared_value = Decimal("0")
    for index, row in enumerate(rows[:20], start=1):
        declared_value = row["declared_value"]
        if declared_value not in ("", None):
            try:
                total_declared_value += Decimal(declared_value)
            except (InvalidOperation, TypeError, ValueError):
                pass

        prefix = f"coin_{index:02d}"
        field_values.update(
            {
                f"{prefix}_date": row["date_mm"],
                f"{prefix}_denom": row["denomination"],
                f"{prefix}_ms_pf": "",
                f"{prefix}_grade": row["grade"],
                f"{prefix}_service": row["holder"],
                f"{prefix}_variety": "",
                f"{prefix}_cert_number": "",
                f"{prefix}_declared_value": _format_declared_value(declared_value),
            }
        )
    return field_values


def _cacg_field_values(submission: Submission, rows: list[dict]) -> dict[str, str]:
    field_values = {
        "service_program_us": "Yes",
        "service_type_grading": "Yes",
    }
    for index, row in enumerate(rows[:20], start=1):
        declared_value = row["declared_value"]
        prefix = f"coin_{index:02d}"
        field_values.update(
            {
                f"{prefix}_date": row["date_mm"],
                f"{prefix}_denom": row["denomination"],
                f"{prefix}_description": "",
                f"{prefix}_current_grade": row["grade"],
                f"{prefix}_cert_number": "",
                f"{prefix}_minimum_grade": "",
                f"{prefix}_declared_value": _format_declared_value(declared_value),
            }
        )
    return field_values


@login_required
def submission_cac_pdf(request: HttpRequest, submission_id: int):
    submission = get_object_or_404(_submission_stable_queryset(), pk=submission_id)
    rows = _submission_export_rows(submission)
    return _fillable_submission_form_response(
        "cac_stickering_submission.pdf",
        submission,
        _cac_field_values(submission, rows),
        "CAC",
        draw_visible_values=False,
    )


@login_required
def submission_cacg_pdf(request: HttpRequest, submission_id: int):
    submission = get_object_or_404(_submission_stable_queryset(), pk=submission_id)
    rows = _submission_export_rows(submission)
    return _fillable_submission_form_response(
        "cacg_submission.pdf",
        submission,
        _cacg_field_values(submission, rows),
        "CACG",
        draw_visible_values=False,
    )
