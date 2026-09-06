from django.contrib import admin
from django.contrib import messages
import csv
from datetime import datetime, time
from decimal import Decimal
from io import BytesIO, StringIO
import html
import zipfile
from django.db import connection
from django.db.utils import DatabaseError
from django.db.models import Count, OuterRef, Subquery, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

from .models import Location, IncomingInventoryBatch, IncomingInventoryLine, InventoryItem, ItemPhoto, Certification, Submission, SubmissionItem, CrackoutEvent, Sale, SaleItem, SaleTube, Container, Product, Report, _next_code
from .views import _ensure_container_table_shape, item_labels_pdf_response, tube_labels_pdf_response


PORTALAPP_ADMIN_ACTIONS_JS = "portalapp/admin_inventory_actions.js"


class PortalBulkActionsMixin:
    class Media:
        js = (PORTALAPP_ADMIN_ACTIONS_JS,)


class TubeSaleStatusListFilter(admin.SimpleListFilter):
    title = "sale status"
    parameter_name = "sold_status"

    def lookups(self, request, model_admin):
        return (
            ("in_stock", "In Stock"),
            ("sold", "Sold"),
        )

    def queryset(self, request, queryset):
        if self.value() == "in_stock":
            return queryset.filter(sale_lines__isnull=True)
        if self.value() == "sold":
            return queryset.filter(sale_lines__isnull=False).distinct()
        return queryset


def _ensure_incoming_inventory_tables():
    existing_tables = set(connection.introspection.table_names())
    with connection.schema_editor() as schema_editor:
        if IncomingInventoryBatch._meta.db_table not in existing_tables:
            schema_editor.create_model(IncomingInventoryBatch)
            existing_tables.add(IncomingInventoryBatch._meta.db_table)
        if IncomingInventoryLine._meta.db_table not in existing_tables:
            schema_editor.create_model(IncomingInventoryLine)


def _ensure_product_table():
    existing_tables = set(connection.introspection.table_names())
    if Product._meta.db_table not in existing_tables:
        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(Product)
        return

    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor,
                Product._meta.db_table,
            )
        }

    missing_fields = [
        field_name
        for field_name in ("cost_basis",)
        if Product._meta.get_field(field_name).column not in columns
    ]
    if not missing_fields:
        return

    with connection.schema_editor() as schema_editor:
        for field_name in missing_fields:
            schema_editor.add_field(Product, Product._meta.get_field(field_name))


def _money(value):
    if value in (None, ""):
        value = Decimal("0.00")
    return f"${Decimal(value):,.2f}"


def _sum_money(values):
    total = Decimal("0.00")
    for value in values:
        if value not in (None, ""):
            total += Decimal(value)
    return total


def _age_days(value):
    if not value:
        return ""
    now = timezone.now()
    if not hasattr(value, "hour"):
        value = timezone.make_aware(
            datetime.combine(value, time.min),
            timezone.get_current_timezone(),
        )
    return max((now - value).days, 0)


def _aging_summary(objects, date_attr="created_at"):
    buckets = {
        "0-30 days": 0,
        "31-60 days": 0,
        "61-90 days": 0,
        "91+ days": 0,
    }
    for obj in objects:
        days = _age_days(getattr(obj, date_attr, None))
        if days == "":
            continue
        if days <= 30:
            buckets["0-30 days"] += 1
        elif days <= 60:
            buckets["31-60 days"] += 1
        elif days <= 90:
            buckets["61-90 days"] += 1
        else:
            buckets["91+ days"] += 1
    return list(buckets.items())


def _report_filename(report, extension):
    return f"{report['key']}-report.{extension}"


def _report_csv_response(context):
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow([context["title"]])
    writer.writerow([])
    writer.writerow(["Summary"])
    writer.writerows(context["summary"])
    if context["aging_summary"]:
        writer.writerow([])
        writer.writerow(["Aging Summary"])
        writer.writerows(context["aging_summary"])
    writer.writerow([])
    writer.writerow(context["headers"])
    writer.writerows(context["rows"])
    response = HttpResponse(out.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{_report_filename(context["report"], "csv")}"'
    return response


def _xlsx_cell(value, cell_type="inlineStr"):
    value = "" if value is None else str(value)
    escaped = html.escape(value)
    if cell_type == "inlineStr":
        return f'<c t="inlineStr"><is><t>{escaped}</t></is></c>'
    return f"<c><v>{escaped}</v></c>"


def _xlsx_row(values):
    return "<row>" + "".join(_xlsx_cell(value) for value in values) + "</row>"


def _report_xlsx_response(context):
    rows = [
        [context["title"]],
        [],
        ["Summary"],
        *([list(row) for row in context["summary"]]),
    ]
    if context["aging_summary"]:
        rows.extend([[], ["Aging Summary"], *([list(row) for row in context["aging_summary"]])])
    rows.extend([[], context["headers"], *context["rows"]])
    sheet_data = "".join(_xlsx_row(row) for row in rows)
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{sheet_data}</sheetData>"
        "</worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Report" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )

    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{_report_filename(context["report"], "xlsx")}"'
    return response


def _report_pdf_response(context):
    buf = BytesIO()
    pdf = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    margin = 0.5 * inch
    y = height - margin

    def new_page():
        nonlocal y
        pdf.showPage()
        y = height - margin

    def draw_line(text, font="Helvetica", size=9, gap=0.18 * inch):
        nonlocal y
        if y < margin + gap:
            new_page()
        pdf.setFont(font, size)
        pdf.drawString(margin, y, str(text))
        y -= gap

    draw_line(context["title"], "Helvetica-Bold", 16, 0.24 * inch)
    draw_line(context["report"]["description"], "Helvetica", 9, 0.28 * inch)
    draw_line("Summary", "Helvetica-Bold", 11)
    for label, value in context["summary"]:
        draw_line(f"{label}: {value}", "Helvetica", 9)
    if context["aging_summary"]:
        y -= 0.08 * inch
        draw_line("Aging Summary", "Helvetica-Bold", 11)
        for label, value in context["aging_summary"]:
            draw_line(f"{label}: {value}", "Helvetica", 9)
    y -= 0.1 * inch

    headers = context["headers"]
    rows = context["rows"]
    available_width = width - (2 * margin)
    header_weights = {
        "ID": 0.8,
        "Product ID": 0.95,
        "Code": 0.8,
        "Coin": 1.8,
        "Name": 1.5,
        "Description": 1.8,
        "Grading Company": 1.15,
        "Cert Number": 1.0,
        "Declared Value": 1.0,
        "Total Value": 1.0,
        "Location": 1.2,
        "Age": 0.65,
        "Margin": 0.7,
    }
    weights = [header_weights.get(header, 1.0) for header in headers]
    weight_total = sum(weights) or 1.0
    col_widths = [(available_width * weight) / weight_total for weight in weights]
    col_positions = [margin]
    for column_width in col_widths[:-1]:
        col_positions.append(col_positions[-1] + column_width)

    def fit_cell_text(text, max_width, font_name="Helvetica", font_size=6.5, max_chars=40):
        text = str(text)
        if pdf.stringWidth(text, font_name, font_size) <= max_width:
            return text
        clipped = text[:max_chars]
        while clipped and pdf.stringWidth(f"{clipped}...", font_name, font_size) > max_width:
            clipped = clipped[:-1]
        return f"{clipped}..." if clipped else ""

    def draw_table_header():
        nonlocal y
        if y < margin + 0.4 * inch:
            new_page()
        pdf.setFont("Helvetica-Bold", 6.5)
        for index, header in enumerate(headers):
            pdf.drawString(
                col_positions[index],
                y,
                fit_cell_text(header, col_widths[index] - 2, "Helvetica-Bold", 6.5, 28),
            )
        y -= 0.12 * inch
        pdf.line(margin, y, width - margin, y)
        y -= 0.12 * inch

    draw_table_header()
    pdf.setFont("Helvetica", 6.5)
    for row in rows:
        if y < margin + 0.25 * inch:
            new_page()
            draw_table_header()
            pdf.setFont("Helvetica", 6.5)
        for index, value in enumerate(row):
            pdf.drawString(
                col_positions[index],
                y,
                fit_cell_text(value, col_widths[index] - 2),
            )
        y -= 0.18 * inch

    pdf.save()
    buf.seek(0)
    response = HttpResponse(buf.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{_report_filename(context["report"], "pdf")}"'
    return response


@admin.register(Location)
class LocationAdmin(PortalBulkActionsMixin, admin.ModelAdmin):
    search_fields = ("name",)


class IncomingInventoryLineInline(admin.TabularInline):
    model = IncomingInventoryLine
    extra = 0
    readonly_fields = ("imported_item", "confidence", "needs_review")
    fields = (
        "date_mm",
        "denomination",
        "series",
        "holder",
        "grade_text",
        "cert_number",
        "cost_basis",
        "ask_price",
        "needs_review",
        "confidence",
        "imported_item",
    )


@admin.register(IncomingInventoryBatch)
class IncomingInventoryBatchAdmin(PortalBulkActionsMixin, admin.ModelAdmin):
    list_display = ("created_at", "title", "vendor", "invoice_number", "parser_status", "line_count")
    list_filter = ("parser_status", "created_at")
    search_fields = ("title", "vendor", "invoice_number", "source_text", "parser_notes")
    readonly_fields = ("created_at", "source_text", "parser_notes")
    inlines = [IncomingInventoryLineInline]

    @admin.display(description="Rows")
    def line_count(self, obj):
        return obj.lines.count()

    def changelist_view(self, request, extra_context=None):
        try:
            _ensure_incoming_inventory_tables()
        except DatabaseError as exc:
            context = {
                **self.admin_site.each_context(request),
                "title": "Incoming inventory setup",
                "error": exc,
            }
            return render(request, "admin/portalapp/incominginventorybatch/setup_error.html", context)
        return super().changelist_view(request, extra_context=extra_context)

    def add_view(self, request, form_url="", extra_context=None):
        _ensure_incoming_inventory_tables()
        return super().add_view(request, form_url=form_url, extra_context=extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        _ensure_incoming_inventory_tables()
        return super().change_view(request, object_id, form_url=form_url, extra_context=extra_context)


class PhotoInline(admin.TabularInline):
    model = ItemPhoto
    extra = 0

class CertInline(admin.TabularInline):
    model = Certification
    extra = 0

@admin.register(InventoryItem)
class InventoryItemAdmin(PortalBulkActionsMixin, admin.ModelAdmin):
    change_list_template = "admin/portalapp/inventoryitem/change_list.html"
    list_display = (
        "internal_id",
        "label_link",
        "denomination",
        "date_mm",
        "series",
        "variety",
        "holder",
        "grade_text",
        "cert_number",
        "ask_price",
        "status",
        "location",
    )
    list_filter = ("holder", "status", "cac_sticker", "location")
    search_fields = (
        "internal_id",
        "denomination",
        "date_mm",
        "series",
        "cert_number",
        "variety",
        "notes",
        "grade_text",
    )
    ordering = ("-created_at",)
    exclude = ("cacg_holder",)
    fields = (
        "internal_id",
        "date_mm",
        "denomination",
        "series",
        "holder",
        "grade_text",
        "cert_number",
        "cac_sticker",
        "variety",
        "notes",
        "ask_price",
        "status",
        "location",
        "show_location",
        "cost_basis",
        "source",
        "acquired_date",
        "created_at",
    )
    inlines = [PhotoInline, CertInline]

    class Media:
        js = ("portalapp/admin_inventory_actions.js",)

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        selected_status = request.GET.get("status__exact", "")
        extra_context["inventory_status_tabs"] = [
            {
                "label": "All",
                "url": ".",
                "active": not selected_status,
            },
            *[
                {
                    "label": label,
                    "url": f".?status__exact={value}",
                    "active": selected_status == value,
                }
                for value, label in InventoryItem.STATUS_CHOICES
            ],
        ]
        return super().changelist_view(request, extra_context=extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "print-labels/",
                self.admin_site.admin_view(self.print_labels_view),
                name="portalapp_inventoryitem_print_labels",
            ),
        ]
        return custom_urls + urls

    @admin.display(description="Label")
    def label_link(self, obj):
        return format_html(
            '<a href="/labels/item/{}.pdf" target="_blank" rel="noopener">Print</a>',
            obj.internal_id,
        )

    def print_labels_view(self, request):
        raw_ids = request.GET.get("ids", "")
        item_ids = []
        for raw_id in raw_ids.split(","):
            raw_id = raw_id.strip()
            if raw_id.isdigit():
                item_ids.append(int(raw_id))

        if not item_ids:
            self.message_user(request, "Select one or more inventory items first.", level=messages.WARNING)
            return redirect("..")

        items_by_id = InventoryItem.objects.in_bulk(item_ids)
        items = [items_by_id[item_id] for item_id in item_ids if item_id in items_by_id]
        if not items:
            self.message_user(request, "No matching inventory items found.", level=messages.WARNING)
            return redirect("..")

        filename = "inventory-labels.pdf" if len(items) > 1 else f"{items[0].internal_id}.pdf"
        return item_labels_pdf_response(items, filename)

@admin.register(Submission)
class SubmissionAdmin(PortalBulkActionsMixin, admin.ModelAdmin):
    list_display = ("internal_id","packet_link","service","status","created_at")
    list_filter = ("service","status")
    search_fields = ("internal_id","notes")
    fields = ("internal_id", "service", "status", "notes")

    class Media:
        js = (PORTALAPP_ADMIN_ACTIONS_JS, "portalapp/admin_submission_packet.js")

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "delete-selected/",
                self.admin_site.admin_view(self.delete_selected_view),
                name="portalapp_submission_delete_selected",
            ),
        ]
        return custom_urls + urls

    def get_queryset(self, request):
        return super().get_queryset(request).only("id", "internal_id", "service", "status", "created_at", "notes")

    @admin.display(description="Packet")
    def packet_link(self, obj):
        return format_html(
            '<a href="/submissions/{}/" target="_blank" rel="noopener">Open</a>',
            obj.id,
        )

    def _submission_table_columns(self):
        with connection.cursor() as cursor:
            return {
                column.name
                for column in connection.introspection.get_table_description(cursor, "portalapp_submission")
            }

    def save_model(self, request, obj, form, change):
        if not obj.internal_id:
            obj.internal_id = _next_code("SUB", Submission, "internal_id")

        values = {
            "internal_id": obj.internal_id,
            "service": obj.service,
            "status": obj.status,
            "notes": obj.notes,
            "grading_submission_number": "",
            "submission_method": "SHIPPED",
            "carrier": "",
            "tracking_number": "",
            "show_name": "",
        }
        table_columns = self._submission_table_columns()
        writable_values = {
            column: value
            for column, value in values.items()
            if column in table_columns
        }

        if change:
            assignments = ", ".join(f"{column} = %s" for column in writable_values)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE portalapp_submission SET {assignments} WHERE id = %s",
                    [*writable_values.values(), obj.pk],
                )
            return

        obj.created_at = timezone.now()
        writable_values["created_at"] = obj.created_at
        columns = ", ".join(writable_values)
        placeholders = ", ".join(["%s"] * len(writable_values))
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO portalapp_submission ({columns}) VALUES ({placeholders}) RETURNING id",
                list(writable_values.values()),
            )
            obj.pk = cursor.fetchone()[0]

    def _delete_submission_ids(self, submission_ids):
        submission_ids = [int(submission_id) for submission_id in submission_ids]
        if not submission_ids:
            return

        item_ids = list(
            SubmissionItem.objects.filter(submission_id__in=submission_ids).values_list("item_id", flat=True)
        )
        SubmissionItem.objects.filter(submission_id__in=submission_ids).delete()
        CrackoutEvent.objects.filter(to_submission_id__in=submission_ids).update(to_submission=None)

        placeholders = ", ".join(["%s"] * len(submission_ids))
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM portalapp_submission WHERE id IN ({placeholders})", submission_ids)

        for item in InventoryItem.objects.filter(id__in=item_ids, status="AT_GRADING"):
            if not SubmissionItem.objects.filter(item=item).exists():
                item.status = "IN_STOCK"
                item.save(update_fields=["status"])

    def delete_model(self, request, obj):
        self._delete_submission_ids([obj.pk])

    def delete_queryset(self, request, queryset):
        self._delete_submission_ids(queryset.values_list("pk", flat=True))

    def delete_view(self, request, object_id, extra_context=None):
        submission = get_object_or_404(self.get_queryset(request), pk=object_id)
        if request.method == "POST" and request.POST.get("post"):
            submission_id = submission.pk
            submission_name = submission.internal_id
            self._delete_submission_ids([submission_id])
            self.message_user(request, f"Deleted submission {submission_name}.", level=messages.SUCCESS)
            return redirect(reverse("admin:portalapp_submission_changelist"))

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "submission": submission,
            "title": f"Delete submission {submission.internal_id}",
        }
        return render(request, "admin/portalapp/submission/delete_confirmation.html", context)

    def delete_selected_view(self, request):
        raw_ids = request.GET.get("ids") or request.POST.get("ids") or ""
        submission_ids = [int(raw_id) for raw_id in raw_ids.split(",") if raw_id.strip().isdigit()]
        submissions = list(self.get_queryset(request).filter(pk__in=submission_ids).order_by("internal_id"))
        if not submissions:
            self.message_user(request, "Select one or more submissions first.", level=messages.WARNING)
            return redirect(reverse("admin:portalapp_submission_changelist"))

        if request.method == "POST" and request.POST.get("post"):
            deleted_count = len(submissions)
            self._delete_submission_ids([submission.pk for submission in submissions])
            self.message_user(request, f"Deleted {deleted_count} submission(s).", level=messages.SUCCESS)
            return redirect(reverse("admin:portalapp_submission_changelist"))

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "submissions": submissions,
            "ids": ",".join(str(submission.pk) for submission in submissions),
            "title": f"Delete {len(submissions)} submissions",
        }
        return render(request, "admin/portalapp/submission/delete_confirmation.html", context)

@admin.register(SubmissionItem)
class SubmissionItemAdmin(PortalBulkActionsMixin, admin.ModelAdmin):
    list_display = ("submission_code","item_code","declared_value","created_at")
    search_fields = ("submission__internal_id","item__internal_id")

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "submission":
            kwargs["queryset"] = Submission.objects.only("id", "internal_id").order_by("-created_at")
        elif db_field.name == "item":
            kwargs["queryset"] = InventoryItem.objects.only("id", "internal_id").order_by("-created_at")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                submission_internal_id=Subquery(
                    Submission.objects.filter(pk=OuterRef("submission_id")).values("internal_id")[:1]
                ),
                item_internal_id=Subquery(
                    InventoryItem.objects.filter(pk=OuterRef("item_id")).values("internal_id")[:1]
                ),
            )
        )

    @admin.display(description="Submission", ordering="submission_internal_id")
    def submission_code(self, obj):
        return obj.submission_internal_id or obj.submission_id

    @admin.display(description="Item", ordering="item_internal_id")
    def item_code(self, obj):
        return obj.item_internal_id or obj.item_id

@admin.register(CrackoutEvent)
class CrackoutAdmin(PortalBulkActionsMixin, admin.ModelAdmin):
    list_display = ("item","from_service","from_grade","outcome","created_at")
    search_fields = ("item__internal_id","from_cert","reason","outcome")
    fields = ("item", "from_service", "from_grade", "from_cert", "to_submission", "reason", "outcome")

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "item":
            kwargs["queryset"] = InventoryItem.objects.only("id", "internal_id").order_by("-created_at")
        elif db_field.name == "to_submission":
            kwargs["queryset"] = Submission.objects.only("id", "internal_id").order_by("-created_at")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

@admin.register(Sale)
class SaleAdmin(PortalBulkActionsMixin, admin.ModelAdmin):
    list_display = ("internal_id","venue","created_at")
    search_fields = ("internal_id","venue","notes")


@admin.register(Product)
class ProductAdmin(PortalBulkActionsMixin, admin.ModelAdmin):
    list_display = ("internal_id", "name", "sku", "quantity", "cost_basis", "unit_price", "location", "updated_at")
    list_filter = ("location",)
    search_fields = ("internal_id", "name", "sku", "notes")
    readonly_fields = ("created_at", "updated_at")
    fields = (
        "internal_id",
        "name",
        "sku",
        "quantity",
        "cost_basis",
        "unit_price",
        "location",
        "notes",
        "created_at",
        "updated_at",
    )

    def changelist_view(self, request, extra_context=None):
        _ensure_product_table()
        return super().changelist_view(request, extra_context=extra_context)

    def add_view(self, request, form_url="", extra_context=None):
        _ensure_product_table()
        return super().add_view(request, form_url=form_url, extra_context=extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        _ensure_product_table()
        return super().change_view(request, object_id, form_url=form_url, extra_context=extra_context)


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    change_list_template = "admin/portalapp/report/change_list.html"
    report_template = "admin/portalapp/report/detail.html"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_staff

    def has_delete_permission(self, request, obj=None):
        return False

    def get_model_perms(self, request):
        if not self.has_change_permission(request):
            return {}
        return {"view": True}

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<str:report_type>/<str:export_format>/",
                self.admin_site.admin_view(self.report_export_view),
                name="portalapp_report_export",
            ),
            path(
                "<str:report_type>/",
                self.admin_site.admin_view(self.report_view),
                name="portalapp_report_detail",
            ),
        ]
        return custom_urls + urls

    def _report_links(self):
        return [
            {
                "key": "inventory",
                "title": "Inventory",
                "description": "All coin inventory with status, grading company, grade, cert, ask price, and location.",
            },
            {
                "key": "tubes",
                "title": "Tubes",
                "description": "Tube inventory with in-stock and sold views.",
            },
            {
                "key": "products",
                "title": "Products",
                "description": "Quantity-based product inventory with SKU, unit price, and location.",
            },
            {
                "key": "submissions",
                "title": "Submissions",
                "description": "Submission packets by service and status.",
            },
            {
                "key": "submission-items",
                "title": "Submission Items",
                "description": "Coins attached to grading submissions with declared values.",
            },
            {
                "key": "sales",
                "title": "Sales",
                "description": "Completed sales and invoice history.",
            },
            {
                "key": "profit-loss",
                "title": "Profit & Loss",
                "description": "Sold inventory revenue, cost basis, gross profit, and margin.",
            },
            {
                "key": "incoming",
                "title": "Incoming Inventory",
                "description": "Uploaded invoice batches and imported inventory rows.",
            },
            {
                "key": "crackouts",
                "title": "Crackout Events",
                "description": "Crackout workflow history and submission routing.",
            },
            {
                "key": "locations",
                "title": "Locations",
                "description": "Inventory location list for office, show, and storage tracking.",
            },
        ]

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["report_links"] = [
            {
                **report,
                "url": reverse("admin:portalapp_report_detail", kwargs={"report_type": report["key"]}),
            }
            for report in self._report_links()
        ]
        extra_context["title"] = "Reports"
        context = {
            **self.admin_site.each_context(request),
            **extra_context,
            "opts": self.model._meta,
        }
        return render(request, self.change_list_template, context)

    def _build_report_context(self, request, report_type):
        report = next((item for item in self._report_links() if item["key"] == report_type), None)
        if report is None:
            return None

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": f"{report['title']} Report",
            "report": report,
            "headers": [],
            "rows": [],
            "summary": [],
            "aging_summary": [],
        }

        if report_type == "inventory":
            items = list(InventoryItem.objects.select_related("location").order_by("-created_at", "internal_id")[:500])
            full_queryset = InventoryItem.objects.select_related("location").order_by("-created_at", "internal_id")
            ask_total = _sum_money(item.ask_price for item in full_queryset)
            cost_total = _sum_money(item.cost_basis for item in full_queryset)
            context["summary"] = [
                ("Total items", full_queryset.count()),
                ("Total ask value", _money(ask_total)),
                ("Total cost", _money(cost_total)),
                ("Potential gross", _money(ask_total - cost_total)),
                ("In stock", full_queryset.filter(status="IN_STOCK").count()),
                ("At grading", full_queryset.filter(status="AT_GRADING").count()),
                ("Sold", full_queryset.filter(status="SOLD").count()),
            ]
            context["aging_summary"] = _aging_summary(items)
            context["headers"] = [
                "ID",
                "Coin",
                "Grading Company",
                "Grade",
                "Cert Number",
                "Cost",
                "Ask",
                "Status",
                "Location",
                "Age",
            ]
            context["rows"] = [
                [
                    item.internal_id,
                    " ".join(part for part in [item.date_mm, item.denomination, item.series] if part),
                    item.holder,
                    item.grade_text,
                    item.cert_number,
                    _money(item.cost_basis) if item.cost_basis is not None else "",
                    _money(item.ask_price) if item.ask_price is not None else "",
                    item.get_status_display(),
                    item.location or item.show_location,
                    f"{_age_days(item.created_at)} days",
                ]
                for item in items
            ]
        elif report_type == "tubes":
            _ensure_container_table_shape()
            tubes = list(Container.objects.order_by("-created_at", "internal_id")[:500])
            queryset = Container.objects.order_by("-created_at", "internal_id")
            sold_ids = SaleTube.objects.values("tube_id")
            ask_total = _sum_money(tube.ask_price for tube in queryset)
            cost_total = _sum_money(tube.cost_basis for tube in queryset)
            context["summary"] = [
                ("Total tubes", queryset.count()),
                ("Total quantity", sum(tube.quantity for tube in queryset)),
                ("Total ask value", _money(ask_total)),
                ("Total cost", _money(cost_total)),
                ("Potential gross", _money(ask_total - cost_total)),
                ("In stock", queryset.exclude(id__in=sold_ids).count()),
                ("Sold", queryset.filter(id__in=sold_ids).count()),
            ]
            context["aging_summary"] = _aging_summary(tubes)
            context["headers"] = ["ID", "Label", "Quantity", "Cost", "Ask", "Status", "Created", "Age"]
            context["rows"] = [
                [
                    tube.internal_id,
                    tube.display_label_text(),
                    tube.quantity,
                    _money(tube.cost_basis) if tube.cost_basis is not None else "",
                    _money(tube.ask_price) if tube.ask_price is not None else "",
                    "Sold" if tube.sale_lines.exists() else "In Stock",
                    timezone.localtime(tube.created_at).strftime("%b %-d, %Y"),
                    f"{_age_days(tube.created_at)} days",
                ]
                for tube in tubes
            ]
        elif report_type == "products":
            _ensure_product_table()
            products = list(Product.objects.select_related("location").order_by("name", "internal_id")[:500])
            queryset = Product.objects.select_related("location").order_by("name", "internal_id")
            total_value = sum(
                Decimal(product.quantity) * Decimal(product.unit_price or 0)
                for product in queryset
            )
            total_cost = sum(
                Decimal(product.quantity) * Decimal(product.cost_basis or 0)
                for product in queryset
            )
            context["summary"] = [
                ("Total products", queryset.count()),
                ("Total quantity", queryset.aggregate(total=Sum("quantity"))["total"] or 0),
                ("Total value", _money(total_value)),
                ("Total cost", _money(total_cost)),
                ("Potential gross", _money(total_value - total_cost)),
            ]
            context["aging_summary"] = _aging_summary(products)
            context["headers"] = ["Product ID", "Name", "SKU", "Quantity", "Cost", "Unit Price", "Total Value", "Location", "Updated", "Age"]
            context["rows"] = [
                [
                    product.internal_id,
                    product.name,
                    product.sku,
                    product.quantity,
                    _money(product.cost_basis) if product.cost_basis is not None else "",
                    _money(product.unit_price) if product.unit_price is not None else "",
                    _money(Decimal(product.quantity) * Decimal(product.unit_price or 0)),
                    product.location or "",
                    timezone.localtime(product.updated_at).strftime("%b %-d, %Y"),
                    f"{_age_days(product.created_at)} days",
                ]
                for product in products
            ]
        elif report_type == "submissions":
            submissions = list(
                Submission.objects.annotate(item_count=Count("lines")).order_by("-created_at", "internal_id")[:500]
            )
            queryset = Submission.objects.annotate(item_count=Count("lines")).order_by("-created_at", "internal_id")
            declared_total = _sum_money(
                SubmissionItem.objects.filter(submission__in=queryset).values_list("declared_value", flat=True)
            )
            context["summary"] = [
                ("Total submissions", queryset.count()),
                ("Total declared value", _money(declared_total)),
                ("Total cost", "N/A"),
                ("Prepared", queryset.filter(status="PREPARED").count()),
            ]
            context["aging_summary"] = _aging_summary(submissions)
            context["headers"] = ["ID", "Service", "Status", "Items", "Declared Value", "Created", "Age"]
            context["rows"] = [
                [
                    submission.internal_id,
                    submission.service,
                    submission.status,
                    submission.item_count,
                    _money(_sum_money(line.declared_value for line in submission.lines.all())),
                    timezone.localtime(submission.created_at).strftime("%b %-d, %Y"),
                    f"{_age_days(submission.created_at)} days",
                ]
                for submission in submissions
            ]
        elif report_type == "submission-items":
            lines = list(SubmissionItem.objects.select_related("submission", "item").order_by("-created_at", "id")[:500])
            queryset = SubmissionItem.objects.select_related("submission", "item").order_by("-created_at", "id")
            declared_total = _sum_money(line.declared_value for line in queryset)
            cost_total = _sum_money(line.item.cost_basis for line in queryset)
            context["summary"] = [
                ("Total submission items", queryset.count()),
                ("Total declared value", _money(declared_total)),
                ("Total item cost", _money(cost_total)),
            ]
            context["aging_summary"] = _aging_summary(lines)
            context["headers"] = ["Submission", "Item", "Coin", "Cost", "Declared Value", "Status", "Created", "Age"]
            context["rows"] = [
                [
                    line.submission.internal_id,
                    line.item.internal_id,
                    " ".join(part for part in [line.item.date_mm, line.item.denomination, line.item.series] if part),
                    _money(line.item.cost_basis) if line.item.cost_basis is not None else "",
                    _money(line.declared_value) if line.declared_value is not None else "",
                    line.item.get_status_display(),
                    timezone.localtime(line.created_at).strftime("%b %-d, %Y"),
                    f"{_age_days(line.created_at)} days",
                ]
                for line in lines
            ]
        elif report_type == "sales":
            sales = list(Sale.objects.annotate(item_count=Count("lines"), tube_count=Count("tube_lines")).order_by("-created_at")[:500])
            queryset = Sale.objects.annotate(item_count=Count("lines"), tube_count=Count("tube_lines")).order_by("-created_at")
            item_total = _sum_money(SaleItem.objects.filter(sale__in=queryset).values_list("sold_price", flat=True))
            tube_total = _sum_money(SaleTube.objects.filter(sale__in=queryset).values_list("sold_price", flat=True))
            context["summary"] = [
                ("Total sales", queryset.count()),
                ("Total sold value", _money(item_total + tube_total)),
                ("Inventory sold value", _money(item_total)),
                ("Tube sold value", _money(tube_total)),
            ]
            context["aging_summary"] = _aging_summary(sales)
            context["headers"] = ["Sale", "Venue", "Items", "Tubes", "Sold Value", "Created", "Age"]
            context["rows"] = [
                [
                    sale.internal_id,
                    sale.venue,
                    sale.item_count,
                    sale.tube_count,
                    _money(
                        _sum_money(line.sold_price for line in sale.lines.all())
                        + _sum_money(line.sold_price for line in sale.tube_lines.all())
                    ),
                    timezone.localtime(sale.created_at).strftime("%b %-d, %Y"),
                    f"{_age_days(sale.created_at)} days",
                ]
                for sale in sales
            ]
        elif report_type == "profit-loss":
            sale_items = list(
                SaleItem.objects.select_related("sale", "item").order_by("-sale__created_at", "id")[:500]
            )
            all_sale_items = SaleItem.objects.select_related("sale", "item").all()
            revenue_total = _sum_money(line.sold_price for line in all_sale_items)
            cost_total = _sum_money(line.item.cost_basis for line in all_sale_items)
            gross_profit = revenue_total - cost_total
            margin = f"{((gross_profit / revenue_total) * Decimal('100')):.1f}%" if revenue_total else "0.0%"
            context["summary"] = [
                ("Sold inventory items", all_sale_items.count()),
                ("Revenue", _money(revenue_total)),
                ("Cost basis", _money(cost_total)),
                ("Gross profit", _money(gross_profit)),
                ("Gross margin", margin),
            ]
            context["aging_summary"] = _aging_summary([line.sale for line in sale_items])
            context["headers"] = ["Sale", "Sold Date", "Item", "Coin", "Revenue", "Cost", "Profit", "Margin"]
            context["rows"] = []
            for line in sale_items:
                revenue = Decimal(line.sold_price or 0)
                cost = Decimal(line.item.cost_basis or 0)
                profit = revenue - cost
                row_margin = f"{((profit / revenue) * Decimal('100')):.1f}%" if revenue else "0.0%"
                context["rows"].append(
                    [
                        line.sale.internal_id,
                        timezone.localtime(line.sale.created_at).strftime("%b %-d, %Y"),
                        line.item.internal_id,
                        " ".join(part for part in [line.item.date_mm, line.item.denomination, line.item.series] if part),
                        _money(revenue),
                        _money(cost),
                        _money(profit),
                        row_margin,
                    ]
                )
        elif report_type == "incoming":
            _ensure_incoming_inventory_tables()
            batches = list(IncomingInventoryBatch.objects.annotate(line_count=Count("lines")).order_by("-created_at")[:500])
            queryset = IncomingInventoryBatch.objects.annotate(line_count=Count("lines")).order_by("-created_at")
            incoming_lines = IncomingInventoryLine.objects.filter(batch__in=queryset)
            context["summary"] = [
                ("Total incoming batches", queryset.count()),
                ("Total rows", incoming_lines.count()),
                ("Total ask value", _money(_sum_money(incoming_lines.values_list("ask_price", flat=True)))),
                ("Total cost", _money(_sum_money(incoming_lines.values_list("cost_basis", flat=True)))),
            ]
            context["aging_summary"] = _aging_summary(batches)
            context["headers"] = ["Batch", "Vendor", "Invoice", "Status", "Rows", "Ask Value", "Cost", "Created", "Age"]
            context["rows"] = [
                [
                    batch.title or str(batch),
                    batch.vendor,
                    batch.invoice_number,
                    batch.get_parser_status_display(),
                    batch.line_count,
                    _money(_sum_money(line.ask_price for line in batch.lines.all())),
                    _money(_sum_money(line.cost_basis for line in batch.lines.all())),
                    timezone.localtime(batch.created_at).strftime("%b %-d, %Y"),
                    f"{_age_days(batch.created_at)} days",
                ]
                for batch in batches
            ]
        elif report_type == "crackouts":
            events = list(CrackoutEvent.objects.select_related("item", "to_submission").order_by("-created_at")[:500])
            queryset = CrackoutEvent.objects.select_related("item", "to_submission").order_by("-created_at")
            context["summary"] = [
                ("Total crackout events", queryset.count()),
                ("Total item ask value", _money(_sum_money(event.item.ask_price for event in queryset))),
                ("Total item cost", _money(_sum_money(event.item.cost_basis for event in queryset))),
            ]
            context["aging_summary"] = _aging_summary(events)
            context["headers"] = ["Item", "From Service", "From Grade", "To Submission", "Cost", "Ask", "Outcome", "Created", "Age"]
            context["rows"] = [
                [
                    event.item.internal_id,
                    event.from_service,
                    event.from_grade,
                    event.to_submission.internal_id if event.to_submission else "",
                    _money(event.item.cost_basis) if event.item.cost_basis is not None else "",
                    _money(event.item.ask_price) if event.item.ask_price is not None else "",
                    event.outcome,
                    timezone.localtime(event.created_at).strftime("%b %-d, %Y"),
                    f"{_age_days(event.created_at)} days",
                ]
                for event in events
            ]
        elif report_type == "locations":
            _ensure_product_table()
            locations = list(Location.objects.annotate(item_count=Count("items"), product_count=Count("products")).order_by("name")[:500])
            queryset = Location.objects.annotate(item_count=Count("items"), product_count=Count("products")).order_by("name")
            context["summary"] = [
                ("Total locations", queryset.count()),
                ("Inventory ask value", _money(_sum_money(item.ask_price for item in InventoryItem.objects.filter(location__in=queryset)))),
                ("Inventory cost", _money(_sum_money(item.cost_basis for item in InventoryItem.objects.filter(location__in=queryset)))),
            ]
            context["headers"] = ["Location", "Inventory Items", "Products", "Inventory Ask", "Inventory Cost"]
            context["rows"] = [
                [
                    location.name,
                    location.item_count,
                    location.product_count,
                    _money(_sum_money(item.ask_price for item in location.items.all())),
                    _money(_sum_money(item.cost_basis for item in location.items.all())),
                ]
                for location in locations
            ]

        context["export_links"] = [
            ("CSV", reverse("admin:portalapp_report_export", kwargs={"report_type": report_type, "export_format": "csv"})),
            ("Excel", reverse("admin:portalapp_report_export", kwargs={"report_type": report_type, "export_format": "xlsx"})),
            ("PDF", reverse("admin:portalapp_report_export", kwargs={"report_type": report_type, "export_format": "pdf"})),
        ]
        return context

    def report_view(self, request, report_type):
        context = self._build_report_context(request, report_type)
        if context is None:
            self.message_user(request, "Choose a valid report.", level=messages.WARNING)
            return redirect(reverse("admin:portalapp_report_changelist"))
        return render(request, self.report_template, context)

    def report_export_view(self, request, report_type, export_format):
        context = self._build_report_context(request, report_type)
        if context is None:
            self.message_user(request, "Choose a valid report.", level=messages.WARNING)
            return redirect(reverse("admin:portalapp_report_changelist"))
        if export_format == "csv":
            return _report_csv_response(context)
        if export_format == "xlsx":
            return _report_xlsx_response(context)
        if export_format == "pdf":
            return _report_pdf_response(context)
        self.message_user(request, "Choose CSV, Excel, or PDF.", level=messages.WARNING)
        return redirect(reverse("admin:portalapp_report_detail", kwargs={"report_type": report_type}))


@admin.register(Container)
class ContainerAdmin(PortalBulkActionsMixin, admin.ModelAdmin):
    change_list_template = "admin/portalapp/container/change_list.html"
    list_display = ("internal_id","date_mm","denomination","series","label_text","quantity","cost_basis","ask_price","created_at")
    list_filter = (TubeSaleStatusListFilter,)
    search_fields = ("internal_id","date_mm","denomination","series","label_text","notes")
    fields = (
        "internal_id",
        "date_mm",
        "denomination",
        "series",
        "label_text",
        "quantity",
        "cost_basis",
        "ask_price",
        "notes",
        "created_at",
    )

    def changelist_view(self, request, extra_context=None):
        _ensure_container_table_shape()
        extra_context = extra_context or {}
        selected_status = request.GET.get("sold_status", "")
        extra_context["tube_status_tabs"] = [
            {
                "label": "All",
                "url": ".",
                "active": not selected_status,
            },
            {
                "label": "In Stock",
                "url": ".?sold_status=in_stock",
                "active": selected_status == "in_stock",
            },
            {
                "label": "Sold",
                "url": ".?sold_status=sold",
                "active": selected_status == "sold",
            },
        ]
        return super().changelist_view(request, extra_context=extra_context)

    def add_view(self, request, form_url="", extra_context=None):
        _ensure_container_table_shape()
        return super().add_view(request, form_url=form_url, extra_context=extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        _ensure_container_table_shape()
        return super().change_view(request, object_id, form_url=form_url, extra_context=extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "print-labels/",
                self.admin_site.admin_view(self.print_labels_view),
                name="portalapp_container_print_labels",
            ),
        ]
        return custom_urls + urls

    def print_labels_view(self, request):
        _ensure_container_table_shape()
        raw_ids = request.GET.get("ids", "")
        tube_ids = []
        for raw_id in raw_ids.split(","):
            raw_id = raw_id.strip()
            if raw_id.isdigit():
                tube_ids.append(int(raw_id))

        if not tube_ids:
            self.message_user(request, "Select one or more tubes first.", level=messages.WARNING)
            return redirect("..")

        tubes_by_id = Container.objects.in_bulk(tube_ids)
        tubes = [tubes_by_id[tube_id] for tube_id in tube_ids if tube_id in tubes_by_id]
        if not tubes:
            self.message_user(request, "No matching tubes found.", level=messages.WARNING)
            return redirect("..")

        filename = "tube-labels.pdf" if len(tubes) > 1 else f"{tubes[0].internal_id}.pdf"
        return tube_labels_pdf_response(tubes, filename)

from django.contrib import admin

admin.site.site_header = "CoinPortal 365 Administration"
admin.site.site_title = "CoinPortal 365"
admin.site.index_title = "CoinPortal Management"
