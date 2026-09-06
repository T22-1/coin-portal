from __future__ import annotations
import csv
import hashlib
import re
from decimal import Decimal, InvalidOperation
from io import BytesIO
from io import StringIO
from pathlib import Path
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.utils import DatabaseError
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateformat import format as date_format
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from reportlab.lib.units import inch
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode import code128
from pypdf import PdfReader, PdfWriter
from pypdf.generic import BooleanObject, NameObject, TextStringObject

from .invoice_parser import parse_invoice_file
from .models import IncomingInventoryBatch, IncomingInventoryLine, InventoryItem, Container, Sale, SaleItem, SaleTube, Submission, SubmissionItem, PricingPlan


ITEM_PREFIXES = ("ID-", "INV-")
SELLABLE_STATUSES = {"IN_STOCK", "LISTED"}
ACTIVE_SUBMISSION_STATUSES = {"PREPARED", "SUBMITTED", "SHIPPED", "AT_GRADING"}
CAC_ALLOWED_HOLDERS = {"PCGS", "NGC"}
INVOICE_BUSINESS_NAME = "TMC Marketplace, Inc."
INVOICE_BUSINESS_ADDRESS_LINES = (
    "1 Chase Corporate Drive",
    "Suite 400",
    "Birmingham, AL 35244",
)
SUBMISSION_SCAN_CODE_RE = re.compile(r"\b(?:ID|INV)-[A-Z0-9-]+\b", re.IGNORECASE)


def _ensure_sale_tube_table() -> None:
    existing_tables = set(connection.introspection.table_names())
    if SaleTube._meta.db_table in existing_tables:
        return
    with connection.schema_editor() as schema_editor:
        schema_editor.create_model(SaleTube)


def _ensure_submission_item_table_shape() -> None:
    existing_tables = set(connection.introspection.table_names())
    if SubmissionItem._meta.db_table not in existing_tables:
        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(SubmissionItem)
        return

    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor,
                SubmissionItem._meta.db_table,
            )
        }

    missing_fields = [
        field_name
        for field_name in ("created_at", "declared_value")
        if SubmissionItem._meta.get_field(field_name).column not in columns
    ]
    if not missing_fields:
        return

    with connection.schema_editor() as schema_editor:
        for field_name in missing_fields:
            schema_editor.add_field(SubmissionItem, SubmissionItem._meta.get_field(field_name))


def _ensure_submission_table_shape() -> None:
    existing_tables = set(connection.introspection.table_names())
    if Submission._meta.db_table not in existing_tables:
        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(Submission)
        return

    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor,
                Submission._meta.db_table,
            )
        }

    missing_fields = [
        field_name
        for field_name in (
            "grading_submission_number",
            "submission_method",
            "carrier",
            "tracking_number",
            "show_name",
        )
        if Submission._meta.get_field(field_name).column not in columns
    ]
    if not missing_fields:
        return

    with connection.schema_editor() as schema_editor:
        for field_name in missing_fields:
            schema_editor.add_field(Submission, Submission._meta.get_field(field_name))


def _ensure_container_table_shape() -> None:
    existing_tables = set(connection.introspection.table_names())
    if Container._meta.db_table not in existing_tables:
        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(Container)
        return

    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor,
                Container._meta.db_table,
            )
        }

    missing_fields = [
        field_name
        for field_name in ("date_mm", "denomination", "series", "cost_basis")
        if Container._meta.get_field(field_name).column not in columns
    ]
    if not missing_fields:
        return

    with connection.schema_editor() as schema_editor:
        for field_name in missing_fields:
            schema_editor.add_field(Container, Container._meta.get_field(field_name))


def _submission_item_table_columns() -> set[str]:
    with connection.cursor() as cursor:
        return {
            column.name
            for column in connection.introspection.get_table_description(
                cursor,
                SubmissionItem._meta.db_table,
            )
        }


def _raw_create_submission_item(submission: Submission, item: InventoryItem, declared_value) -> None:
    columns = _submission_item_table_columns()
    table_name = connection.ops.quote_name(SubmissionItem._meta.db_table)
    insert_columns = ["submission_id", "item_id"]
    values = [submission.id, item.id]

    if "created_at" in columns:
        insert_columns.append("created_at")
        values.append(timezone.now())
    if "declared_value" in columns:
        insert_columns.append("declared_value")
        values.append(declared_value)

    quoted_columns = ", ".join(connection.ops.quote_name(column) for column in insert_columns)
    placeholders = ", ".join(["%s"] * len(values))
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {table_name} ({quoted_columns}) VALUES ({placeholders})",
            values,
        )


def _force_item_onto_submission(submission: Submission, item: InventoryItem, declared_value) -> bool:
    existing_line = (
        SubmissionItem.objects.filter(item=item)
        .only("id", "submission", "item")
        .order_by("-id")
        .first()
    )
    if existing_line:
        existing_line.submission = submission
        existing_line.save(update_fields=["submission"])
        return True

    _raw_create_submission_item(submission, item, declared_value)
    return True


def _sale_price_from_request(request: HttpRequest, code: str, fallback=None):
    price_raw = (request.POST.get(f"price_{code}") or "").strip().replace(",","")
    if not price_raw and fallback is not None:
        return fallback
    if price_raw:
        try:
            return Decimal(price_raw)
        except InvalidOperation:
            return fallback
    return None


def _clean_scan_code(raw_code: str) -> str:
    return (
        (raw_code or "")
        .strip()
        .upper()
        .replace(" ", "")
        .replace("_", "-")
        .replace("–", "-")
        .replace("—", "-")
    )


def _resolve_sale_scan_code(raw_code: str) -> tuple[str, str | None]:
    code = _clean_scan_code(raw_code)
    if code.startswith(ITEM_PREFIXES):
        return "item", code
    if code.startswith("TUBE-"):
        return "tube", code
    if code.isdigit():
        item_code = f"ID-{code}"
        if InventoryItem.objects.filter(internal_id=item_code).exists():
            return "item", item_code
        tube_code = f"TUBE-{code}"
        if Container.objects.filter(internal_id=tube_code).exists():
            return "tube", tube_code
    return "", None

def login_view(request: HttpRequest):
    if request.method == "POST":
        username = request.POST.get("username","")
        password = request.POST.get("password","")
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            return redirect("dashboard")
        return render(request, "login.html", {"error":"Invalid username/password"})
    return render(request, "login.html")

def logout_view(request: HttpRequest):
    logout(request)
    return redirect("login")

def home(request: HttpRequest):
    return pricing(request)


def pricing(request: HttpRequest):
    plans = PricingPlan.objects.filter(is_active=True, is_public=True).order_by("display_order", "price", "name")
    return render(request, "pricing.html", {"plans": plans})


@login_required
def dashboard(request: HttpRequest):
    return render(request, "home.html")


@login_required
def inventory_master_list(request: HttpRequest):
    query = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    holder = (request.GET.get("holder") or "").strip()
    items = InventoryItem.objects.select_related("location").order_by("-created_at", "internal_id")

    if query:
        items = items.filter(
            Q(internal_id__icontains=query)
            | Q(date_mm__icontains=query)
            | Q(denomination__icontains=query)
            | Q(series__icontains=query)
            | Q(variety__icontains=query)
            | Q(holder__icontains=query)
            | Q(grade_text__icontains=query)
            | Q(cert_number__icontains=query)
            | Q(notes__icontains=query)
        )
    if status:
        items = items.filter(status=status)
    if holder:
        items = items.filter(holder__iexact=holder)

    items = items[:250]
    holders = (
        InventoryItem.objects.exclude(holder="")
        .order_by("holder")
        .values_list("holder", flat=True)
        .distinct()
    )
    return render(
        request,
        "inventory_master_list.html",
        {
            "items": items,
            "query": query,
            "selected_status": status,
            "selected_holder": holder,
            "status_choices": InventoryItem.STATUS_CHOICES,
            "status_tabs": [("", "All")] + list(InventoryItem.STATUS_CHOICES),
            "holders": holders,
        },
    )


@login_required
def incoming_inventory(request: HttpRequest):
    batches = (
        IncomingInventoryBatch.objects.annotate(line_count=Count("lines"))
        .order_by("-created_at")[:25]
    )
    return render(request, "incoming_inventory.html", {"batches": batches})


@login_required
@require_http_methods(["POST"])
def incoming_inventory_upload(request: HttpRequest):
    invoice = request.FILES.get("invoice")
    if not invoice:
        messages.warning(request, "Choose an invoice file first.")
        return redirect("incoming_inventory")

    title = (request.POST.get("title") or invoice.name).strip()
    vendor = (request.POST.get("vendor") or "").strip()
    invoice_number = (request.POST.get("invoice_number") or "").strip()
    batch = IncomingInventoryBatch.objects.create(
        title=title,
        vendor=vendor,
        invoice_number=invoice_number,
        source_file=invoice,
    )

    text, rows, notes = parse_invoice_file(batch.source_file)
    batch.source_text = text
    batch.parser_notes = notes
    batch.parser_status = "PARSED" if rows else "NEEDS_REVIEW"
    batch.save(update_fields=["source_text", "parser_notes", "parser_status"])

    for row in rows:
        IncomingInventoryLine.objects.create(batch=batch, **row)

    if rows:
        messages.success(request, f"Found {len(rows)} possible coin rows. Review them before importing.")
    else:
        messages.warning(request, "I could not confidently find coin rows. You can add rows manually on the review screen.")
    return redirect("incoming_inventory_batch", batch_id=batch.id)


@login_required
def incoming_inventory_batch(request: HttpRequest, batch_id: int):
    batch = get_object_or_404(
        IncomingInventoryBatch.objects.prefetch_related("lines__imported_item"),
        pk=batch_id,
    )
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save":
            _save_incoming_lines(request, batch)
            batch.parser_status = "REVIEWED"
            batch.save(update_fields=["parser_status"])
            messages.success(request, "Incoming rows saved.")
            return redirect("incoming_inventory_batch", batch_id=batch.id)
        if action == "import":
            _save_incoming_lines(request, batch)
            imported_count = _import_incoming_lines(request, batch)
            if imported_count:
                batch.parser_status = "IMPORTED"
                batch.save(update_fields=["parser_status"])
                messages.success(request, f"Imported {imported_count} coin(s) into inventory.")
            else:
                messages.warning(request, "No selected rows were ready to import.")
            return redirect("incoming_inventory_batch", batch_id=batch.id)

    return render(request, "incoming_inventory_batch.html", {"batch": batch})


def _save_incoming_lines(request: HttpRequest, batch: IncomingInventoryBatch) -> None:
    for line in batch.lines.all():
        prefix = f"line_{line.id}_"
        line.raw_description = (request.POST.get(prefix + "raw_description") or "").strip()
        line.date_mm = (request.POST.get(prefix + "date_mm") or "").strip()
        line.denomination = (request.POST.get(prefix + "denomination") or "").strip()
        line.series = (request.POST.get(prefix + "series") or "").strip()
        line.variety = (request.POST.get(prefix + "variety") or "").strip()
        line.holder = (request.POST.get(prefix + "holder") or "").strip().upper()
        line.grade_text = (request.POST.get(prefix + "grade_text") or "").strip()
        line.cert_number = (request.POST.get(prefix + "cert_number") or "").strip()
        line.ask_price = _decimal_from_post(request.POST.get(prefix + "ask_price"))
        line.cost_basis = _decimal_from_post(request.POST.get(prefix + "cost_basis"))
        line.source = (request.POST.get(prefix + "source") or batch.vendor or "").strip()
        line.needs_review = not bool(line.date_mm and line.denomination and line.series)
        line.save()


def _import_incoming_lines(request: HttpRequest, batch: IncomingInventoryBatch) -> int:
    imported_count = 0
    selected_ids = {
        int(value)
        for value in request.POST.getlist("selected_lines")
        if value.isdigit()
    }
    for line in batch.lines.filter(id__in=selected_ids, imported_item__isnull=True):
        if line.needs_review:
            continue
        item = InventoryItem.objects.create(
            date_mm=line.date_mm,
            denomination=line.denomination,
            series=line.series,
            variety=line.variety,
            holder=line.holder,
            grade_text=line.grade_text,
            cert_number=line.cert_number,
            ask_price=line.ask_price,
            cost_basis=line.cost_basis,
            source=line.source or batch.vendor,
            notes=f"Imported from incoming batch {batch.id}. {line.raw_description}".strip(),
        )
        line.imported_item = item
        line.save(update_fields=["imported_item"])
        imported_count += 1
    return imported_count


def _decimal_from_post(value):
    value = (value or "").replace("$", "").replace(",", "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


@login_required
def active_submissions(request: HttpRequest):
    submissions = (
        _submission_stable_queryset()
        .filter(status__in=ACTIVE_SUBMISSION_STATUSES)
        .annotate(item_count=Count("lines"))
        .order_by("-created_at", "internal_id")
    )
    return render(
        request,
        "active_submissions.html",
        {
            "submissions": submissions,
            "active_statuses": sorted(ACTIVE_SUBMISSION_STATUSES),
        },
    )

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
    try:
        _ensure_container_table_shape()
    except DatabaseError:
        pass
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
    raw_code = request.POST.get("code") or ""
    code_type, code = _resolve_sale_scan_code(raw_code)
    batch = request.session.get("sale_batch", [])
    if not isinstance(batch, list):
        batch = []
    if code_type == "item" and code:
        try:
            item = InventoryItem.objects.get(internal_id=code)
        except InventoryItem.DoesNotExist:
            messages.warning(request, f"{code} was not found.")
        else:
            if item.status not in SELLABLE_STATUSES:
                messages.warning(request, f"{code} is {item.get_status_display()} and cannot be added to a sale.")
            elif code not in batch:
                batch.append(code)
    elif code_type == "tube" and code:
        if not Container.objects.filter(internal_id=code).exists():
            messages.warning(request, f"{code} was not found.")
        elif code not in batch:
            batch.append(code)
    else:
        cleaned_code = _clean_scan_code(raw_code)
        if cleaned_code:
            messages.warning(request, f"{cleaned_code} was not found.")
    request.session["sale_batch"] = batch
    request.session.modified = True
    return redirect("sale_batch")


@login_required
@require_http_methods(["POST"])
def sale_remove_scan(request: HttpRequest):
    raw_code = request.POST.get("code") or ""
    code_type, code = _resolve_sale_scan_code(raw_code)
    if not code:
        code = _clean_scan_code(raw_code)
    batch = request.session.get("sale_batch", [])
    if not isinstance(batch, list):
        batch = []
    if code in batch:
        batch.remove(code)
        messages.success(request, f"Removed {code} from the sale batch.")
    elif code:
        messages.warning(request, f"{code} was not in the sale batch.")
    request.session["sale_batch"] = batch
    request.session.modified = True
    return redirect("sale_batch")


@login_required
def sale_batch(request: HttpRequest):
    try:
        _ensure_container_table_shape()
    except DatabaseError:
        pass
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
    _ensure_sale_tube_table()
    try:
        _ensure_container_table_shape()
    except DatabaseError:
        pass
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
            sold_price = _sale_price_from_request(request, code, item.ask_price)
            SaleItem.objects.create(sale=sale, item=item, sold_price=sold_price)
            item.status = "SOLD"
            item.save(update_fields=["status"])
        elif code.startswith("TUBE-"):
            try:
                tube = Container.objects.get(internal_id=code)
            except Container.DoesNotExist:
                continue
            sold_price = _sale_price_from_request(request, code, tube.ask_price)
            SaleTube.objects.create(sale=sale, tube=tube, sold_price=sold_price)

    request.session["sale_batch"] = []
    request.session.modified = True
    return redirect("sale_invoice_pdf", sale_id=sale.pk)


@login_required
def sale_invoice_pdf(request: HttpRequest, sale_id: int):
    _ensure_sale_tube_table()
    try:
        _ensure_container_table_shape()
    except DatabaseError:
        pass
    sale = get_object_or_404(Sale, pk=sale_id)
    items = list(sale.lines.select_related("item").order_by("id"))
    tubes = list(sale.tube_lines.select_related("tube").order_by("id"))
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    margin = 0.55 * inch
    y = height - margin

    def money(value) -> str:
        return f"${Decimal(value or 0):,.2f}"

    def draw_header() -> None:
        nonlocal y
        c.setFont("Helvetica-Bold", 16)
        c.drawString(margin, y, INVOICE_BUSINESS_NAME)
        c.setFont("Helvetica", 9)
        for index, line in enumerate(INVOICE_BUSINESS_ADDRESS_LINES, start=1):
            c.drawString(margin, y - (14 * index), line)
        c.setFont("Helvetica-Bold", 18)
        c.drawRightString(width - margin, y, "Invoice")
        c.setFont("Helvetica", 9)
        c.drawRightString(width - margin, y - 14, sale.internal_id)
        c.drawRightString(width - margin, y - 28, date_format(timezone.localtime(sale.created_at), "M j, Y"))
        if sale.venue:
            c.drawRightString(width - margin, y - 42, sale.venue)
        y -= 76

    def draw_table_header() -> None:
        nonlocal y
        code_x = margin
        desc_x = margin + 1.15 * inch
        cert_x = width - margin - 2.75 * inch
        amount_x = width - margin
        c.setFont("Helvetica-Bold", 9)
        c.drawString(code_x, y, "Code")
        c.drawString(desc_x, y, "Description")
        c.drawString(cert_x, y, "Cert Number")
        c.drawRightString(amount_x, y, "Amount")
        y -= 8
        c.line(margin, y, width - margin, y)
        y -= 14

    def ensure_space() -> None:
        nonlocal y
        if y < margin + 0.7 * inch:
            c.showPage()
            y = height - margin
            draw_table_header()

    draw_header()
    draw_table_header()
    total = Decimal("0.00")
    c.setFont("Helvetica", 9)
    code_x = margin
    desc_x = margin + 1.15 * inch
    cert_x = width - margin - 2.75 * inch
    amount_x = width - margin

    for line in items:
        ensure_space()
        item = line.item
        description = " ".join(part for part in [
            item.date_mm,
            item.denomination,
            item.series,
            item.holder,
            item.grade_text,
        ] if part)
        amount = Decimal(line.sold_price or 0)
        total += amount
        c.drawString(code_x, y, item.internal_id)
        c.drawString(desc_x, y, description[:42])
        c.drawString(cert_x, y, (item.cert_number or "N/A")[:18])
        c.drawRightString(amount_x, y, money(amount))
        y -= 18

    for line in tubes:
        ensure_space()
        tube = line.tube
        amount = Decimal(line.sold_price or 0)
        total += amount
        description = tube.display_label_text() or f"Tube quantity {tube.quantity}"
        c.drawString(code_x, y, tube.internal_id)
        c.drawString(desc_x, y, description[:42])
        c.drawString(cert_x, y, "N/A")
        c.drawRightString(amount_x, y, money(amount))
        y -= 18

    y -= 6
    c.line(width - margin - 2.2 * inch, y, width - margin, y)
    y -= 18
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(width - margin - 0.8 * inch, y, "Total")
    c.drawRightString(width - margin, y, money(total))
    c.showPage()
    c.save()
    buf.seek(0)
    return _label_pdf_response(buf, f"{sale.internal_id}-invoice.pdf")

def _label_pdf_response(buf: BytesIO, filename: str) -> HttpResponse:
    resp = HttpResponse(buf.getvalue(), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{filename}"'
    return resp


LABEL_WIDTH = 2 * inch
LABEL_HEIGHT = 0.75 * inch
LABEL_MARGIN_X = 0.07 * inch
LABEL_BARCODE_HEIGHT = 0.20 * inch
LABEL_BARCODE_Y = 0.05 * inch
LABEL_BUSINESS_NAME = "TMC Marketplace, Inc."


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


def _ask_price_label(ask_price) -> str:
    return f"ASK ${ask_price:,.2f}" if ask_price is not None else "ASK $"


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

    _draw_fit_text(c, _ask_price_label(item.ask_price), x_margin, y_top - 0.22 * inch, usable_width, "Helvetica-Bold", 6.5, 5.0)

    _draw_fit_text(c, LABEL_BUSINESS_NAME, x_margin, y_top - 0.31 * inch, usable_width, "Helvetica", 4.8, 4.0)

    # Barcode
    barcode = _fit_code128(item.internal_id, usable_width, 0.0078 * inch, 0.0045 * inch)
    barcode.drawOn(c, x_margin + ((usable_width - barcode.width) / 2), LABEL_BARCODE_Y)

    c.showPage()


@login_required
def label_tube_pdf(request: HttpRequest, code: str):
    try:
        _ensure_container_table_shape()
    except DatabaseError:
        pass
    tube = get_object_or_404(Container, internal_id=code.upper())
    return tube_labels_pdf_response([tube], f"{tube.internal_id}.pdf")


def _draw_tube_label(c: canvas.Canvas, tube: Container) -> None:
    c.setPageSize((LABEL_WIDTH, LABEL_HEIGHT))

    x_margin = LABEL_MARGIN_X
    usable_width = LABEL_WIDTH - (2 * x_margin)
    y_top = 0.58 * inch

    _draw_fit_text(c, tube.internal_id, x_margin, y_top, usable_width, "Helvetica-Bold", 10, 6.0)

    _draw_fit_text(c, tube.display_label_text(), x_margin, y_top - 0.12 * inch, usable_width, "Helvetica", 5.5, 4.5)

    _draw_fit_text(c, _ask_price_label(tube.ask_price), x_margin, y_top - 0.22 * inch, usable_width, "Helvetica-Bold", 6.5, 5.0)

    _draw_fit_text(c, LABEL_BUSINESS_NAME, x_margin, y_top - 0.31 * inch, usable_width, "Helvetica", 4.8, 4.0)

    barcode = _fit_code128(tube.internal_id, usable_width, 0.0078 * inch, 0.0045 * inch)
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


def tube_labels_pdf_response(tubes, filename: str = "tube-labels.pdf") -> HttpResponse:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(LABEL_WIDTH, LABEL_HEIGHT))
    for tube in tubes:
        _draw_tube_label(c, tube)
    c.save()
    buf.seek(0)
    return _label_pdf_response(buf, filename)


def _submission_lines(submission: Submission):
    _ensure_submission_item_table_shape()
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
    _ensure_submission_item_table_shape()
    return SubmissionItem.objects.filter(
        item=item,
        submission__status__in=ACTIVE_SUBMISSION_STATUSES,
    ).select_related("submission").only(
        "id",
        "item",
        "submission",
        "submission__id",
        "submission__internal_id",
        "submission__status",
    )


def _submission_rejection_reason(submission: Submission, item: InventoryItem) -> str:
    active_line = (
        _active_submission_lines_for_item(item)
        .exclude(submission=submission)
        .exclude(submission__status="PREPARED")
        .first()
    )
    if active_line:
        return f"{item.internal_id} is already on active submission {active_line.submission.internal_id}."

    if submission.service == "CAC" and item.holder.upper() not in CAC_ALLOWED_HOLDERS:
        return f"{item.internal_id} cannot be added to CAC unless it is already in a PCGS or NGC holder."

    return ""


def _move_prepared_submission_lines_to_submission(submission: Submission, item: InventoryItem) -> bool:
    _ensure_submission_item_table_shape()
    prepared_lines = list(
        SubmissionItem.objects.filter(
            item=item,
            submission__status="PREPARED",
        )
        .exclude(submission=submission)
        .order_by("created_at", "id")
    )
    if not prepared_lines:
        return False

    line_to_move = prepared_lines[0]
    line_to_move.submission = submission
    line_to_move.save(update_fields=["submission"])
    SubmissionItem.objects.filter(pk__in=[line.pk for line in prepared_lines[1:]]).delete()
    return True


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
    try:
        _ensure_submission_table_shape()
    except DatabaseError:
        pass

    try:
        _ensure_submission_item_table_shape()
    except DatabaseError:
        messages.warning(request, "Submission items database setup failed. Please try again after the database finishes updating.")
        return redirect("submission_packet", submission_id=submission_id)

    submission = get_object_or_404(_submission_stable_queryset(), pk=submission_id)
    raw_codes = (request.POST.get("codes") or "").replace(",", "\n")
    matched_codes = SUBMISSION_SCAN_CODE_RE.findall(raw_codes)
    if matched_codes:
        codes = [code.upper() for code in matched_codes]
    else:
        codes = [code.strip().upper() for code in raw_codes.splitlines() if code.strip()]

    added = 0
    already_present = 0
    not_found = []
    rejected = []
    for code in codes:
        try:
            item = InventoryItem.objects.get(internal_id=code)
            rejection_reason = _submission_rejection_reason(submission, item)
            if rejection_reason:
                rejected.append(rejection_reason)
                continue

            if SubmissionItem.objects.filter(submission=submission, item=item).exists():
                already_present += 1
                continue

            if _move_prepared_submission_lines_to_submission(submission, item):
                item.status = "AT_GRADING"
                item.save(update_fields=["status"])
                added += 1
                continue

            declared_value = item.cost_basis or item.ask_price
            try:
                SubmissionItem.objects.create(
                    submission=submission,
                    item=item,
                    declared_value=declared_value,
                )
            except DatabaseError:
                _force_item_onto_submission(submission, item, declared_value)
            item.status = "AT_GRADING"
            item.save(update_fields=["status"])
            added += 1
        except InventoryItem.DoesNotExist:
            not_found.append(code)
        except DatabaseError as exc:
            rejected.append(f"{code} could not be added because of a database issue: {exc}")
        except Exception as exc:
            rejected.append(f"{code} could not be added: {exc}")

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
