from __future__ import annotations
from decimal import Decimal, InvalidOperation
from io import BytesIO
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode import code128

from .models import InventoryItem, Container, Sale, SaleItem


ITEM_PREFIXES = ("ID-", "INV-")

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
        if code not in batch:
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


def _fit_code128(value: str, max_width: float, max_bar_width: float, min_bar_width: float):
    bar_width = max_bar_width
    while bar_width > min_bar_width:
        barcode = code128.Code128(
            value,
            barHeight=0.20 * inch,
            barWidth=bar_width,
            humanReadable=False,
        )
        if barcode.width <= max_width:
            return barcode
        bar_width -= 0.0004 * inch
    return code128.Code128(
        value,
        barHeight=0.20 * inch,
        barWidth=min_bar_width,
        humanReadable=False,
    )

@login_required
def label_item_pdf(request: HttpRequest, code: str):

    item = get_object_or_404(InventoryItem, internal_id=code.upper())

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(LABEL_WIDTH, LABEL_HEIGHT))

    x_margin = LABEL_MARGIN_X
    usable_width = LABEL_WIDTH - (2 * x_margin)
    y_top = 0.59 * inch

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
    barcode.drawOn(c, x_margin + ((usable_width - barcode.width) / 2), 0.05 * inch)

    c.showPage()
    c.save()
    buf.seek(0)
    return _label_pdf_response(buf, f"{item.internal_id}.pdf")


@login_required
def label_tube_pdf(request: HttpRequest, code: str):
    tube = get_object_or_404(Container, internal_id=code.upper())
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(LABEL_WIDTH, LABEL_HEIGHT))

    x_margin = LABEL_MARGIN_X
    usable_width = LABEL_WIDTH - (2 * x_margin)
    y_top = 0.60 * inch

    _draw_fit_text(c, tube.internal_id, x_margin, y_top, usable_width, "Helvetica-Bold", 10, 6.0)

    _draw_fit_text(c, tube.label_text or "", x_margin, y_top - 0.16 * inch, usable_width, "Helvetica", 7.0, 4.5)

    barcode = _fit_code128(tube.internal_id, usable_width, 0.010 * inch, 0.0045 * inch)
    barcode.drawOn(c, x_margin + ((usable_width - barcode.width) / 2), 0.05 * inch)

    c.showPage()
    c.save()
    buf.seek(0)
    return _label_pdf_response(buf, f"{tube.internal_id}.pdf")
