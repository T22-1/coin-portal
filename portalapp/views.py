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
        if code.upper().startswith(("ID-", "INV-")):
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
    if code.startswith("INV-"):
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
        if code.startswith("INV-"):
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
        if code.startswith("INV-"):
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

@login_required
def label_item_pdf(request: HttpRequest, code: str):
    item = get_object_or_404(InventoryItem, internal_id=code.upper())

    buf = BytesIO()
    # Keep exact label size: 2.00" wide x 0.75" high
    c = canvas.Canvas(buf, pagesize=(2 * inch, 0.75 * inch))

    x_margin = 0.05 * inch
    y_top = 0.68 * inch

    # Line 1: internal id
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(x_margin, y_top, item.internal_id)

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

    line2 = " | ".join(details)[:42]
    c.setFont("Helvetica", 5.5)
    c.drawString(x_margin, y_top - 0.12 * inch, line2)

    # Line 3: ask
    ask = f"ASK ${item.ask_price:,.2f}" if item.ask_price is not None else "ASK $"
    c.setFont("Helvetica-Bold", 6.5)
    c.drawString(x_margin, y_top - 0.22 * inch, ask)

    # Barcode
    barcode = code128.Code128(
        item.internal_id,
        barHeight=0.20 * inch,
        barWidth=0.0078 * inch,
        humanReadable=False,
    )
    barcode.drawOn(c, x_margin, 0.05 * inch)

        c.showPage()
    c.save()
    buf.seek(0)
    return _label_pdf_response(buf, f"{item.internal_id}.pdf")


@login_required
def label_tube_pdf(request: HttpRequest, code: str):
    # Line 3: price
    ask = f"ASK ${item.ask_price:,.2f}" if item.ask_price is not None else "ASK $"
    c.setFont("Helvetica-Bold", 6.5)
    c.drawString(x_margin, y_top - 0.22 * inch, ask)

    # Barcode
    barcode = code128.Code128(
        item.internal_id,
        barHeight=0.20 * inch,
        barWidth=0.0078 * inch,
        humanReadable=False,
    )
    barcode.drawOn(c, x_margin, 0.05 * inch)

    c.showPage()
    c.save()
    buf.seek(0)
    return _label_pdf_response(buf, f"{item.internal_id}.pdf")
# Line 3: ask
ask = f"ASK ${item.ask_price:,.2f}" if item.ask_price is not None else "ASK $"
c.setFont("Helvetica-Bold", 6.5)
c.drawString(x_margin, y_top - 0.22 * inch, ask)

# Barcode
barcode = code128.Code128(
    item.internal_id,
    barHeight=0.20 * inch,
    barWidth=0.0078 * inch,
    humanReadable=False,
)

barcode.drawOn(c, x_margin, 0.05 * inch)

    c.showPage()
    c.save()
    buf.seek(0)
    return _label_pdf_response(buf, f"{item.internal_id}.pdf")

@login_required
def label_tube_pdf(request: HttpRequest, code: str):
    tube = get_object_or_404(Container, internal_id=code.upper())
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(2*inch, 0.75*inch))

    x_margin = 0.08 * inch
    y_top = 0.70 * inch

    c.setFont("Helvetica-Bold", 11)
    c.drawString(x_margin, y_top, tube.internal_id)

    c.setFont("Helvetica", 7.2)
    c.drawString(x_margin, y_top - 0.16*inch, (tube.label_text or "")[:60])

    ask = f"ASK ${tube.ask_price:,.2f}" if tube.ask_price is not None else ""
    c.setFont("Helvetica-Bold", 8.0)
    c.drawString(x_margin, y_top - 0.30*inch, ask)

    bc = code128.Code128(tube.internal_id, barHeight=0.22*inch, barWidth=0.012*inch)
    bc.drawOn(c, x_margin, 0.05*inch)

    c.showPage()
    c.save()
    buf.seek(0)
    return _label_pdf_response(buf, f"{tube.internal_id}.pdf")
