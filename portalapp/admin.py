from django.contrib import admin
from django.contrib import messages
from django.db import connection
from django.db.utils import DatabaseError
from django.db.models import Count, OuterRef, Subquery, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

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
    if Product._meta.db_table in existing_tables:
        return
    with connection.schema_editor() as schema_editor:
        schema_editor.create_model(Product)


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
    list_display = ("internal_id", "name", "sku", "quantity", "unit_price", "location", "updated_at")
    list_filter = ("location",)
    search_fields = ("internal_id", "name", "sku", "notes")
    readonly_fields = ("created_at", "updated_at")
    fields = (
        "internal_id",
        "name",
        "sku",
        "quantity",
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

    def report_view(self, request, report_type):
        report = next((item for item in self._report_links() if item["key"] == report_type), None)
        if report is None:
            self.message_user(request, "Choose a valid report.", level=messages.WARNING)
            return redirect(reverse("admin:portalapp_report_changelist"))

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": f"{report['title']} Report",
            "report": report,
            "headers": [],
            "rows": [],
            "summary": [],
        }

        if report_type == "inventory":
            queryset = InventoryItem.objects.select_related("location").order_by("-created_at", "internal_id")
            context["summary"] = [
                ("Total items", queryset.count()),
                ("In stock", queryset.filter(status="IN_STOCK").count()),
                ("At grading", queryset.filter(status="AT_GRADING").count()),
                ("Sold", queryset.filter(status="SOLD").count()),
            ]
            context["headers"] = ["ID", "Coin", "Grading Company", "Grade", "Cert Number", "Ask", "Status", "Location"]
            context["rows"] = [
                [
                    item.internal_id,
                    " ".join(part for part in [item.date_mm, item.denomination, item.series] if part),
                    item.holder,
                    item.grade_text,
                    item.cert_number,
                    item.ask_price or "",
                    item.get_status_display(),
                    item.location or item.show_location,
                ]
                for item in queryset[:500]
            ]
        elif report_type == "tubes":
            _ensure_container_table_shape()
            queryset = Container.objects.order_by("-created_at", "internal_id")
            sold_ids = SaleTube.objects.values("tube_id")
            context["summary"] = [
                ("Total tubes", queryset.count()),
                ("In stock", queryset.exclude(id__in=sold_ids).count()),
                ("Sold", queryset.filter(id__in=sold_ids).count()),
            ]
            context["headers"] = ["ID", "Label", "Quantity", "Ask", "Status", "Created"]
            context["rows"] = [
                [
                    tube.internal_id,
                    tube.display_label_text(),
                    tube.quantity,
                    tube.ask_price or "",
                    "Sold" if tube.sale_lines.exists() else "In Stock",
                    timezone.localtime(tube.created_at).strftime("%b %-d, %Y"),
                ]
                for tube in queryset[:500]
            ]
        elif report_type == "products":
            _ensure_product_table()
            queryset = Product.objects.select_related("location").order_by("name", "internal_id")
            context["summary"] = [
                ("Total products", queryset.count()),
                ("Total quantity", queryset.aggregate(total=Sum("quantity"))["total"] or 0),
            ]
            context["headers"] = ["Product ID", "Name", "SKU", "Quantity", "Unit Price", "Location", "Updated"]
            context["rows"] = [
                [
                    product.internal_id,
                    product.name,
                    product.sku,
                    product.quantity,
                    product.unit_price or "",
                    product.location or "",
                    timezone.localtime(product.updated_at).strftime("%b %-d, %Y"),
                ]
                for product in queryset[:500]
            ]
        elif report_type == "submissions":
            queryset = Submission.objects.annotate(item_count=Count("lines")).order_by("-created_at", "internal_id")
            context["summary"] = [
                ("Total submissions", queryset.count()),
                ("Prepared", queryset.filter(status="PREPARED").count()),
            ]
            context["headers"] = ["ID", "Service", "Status", "Items", "Created"]
            context["rows"] = [
                [
                    submission.internal_id,
                    submission.service,
                    submission.status,
                    submission.item_count,
                    timezone.localtime(submission.created_at).strftime("%b %-d, %Y"),
                ]
                for submission in queryset[:500]
            ]
        elif report_type == "submission-items":
            queryset = SubmissionItem.objects.select_related("submission", "item").order_by("-created_at", "id")
            context["summary"] = [("Total submission items", queryset.count())]
            context["headers"] = ["Submission", "Item", "Coin", "Declared Value", "Created"]
            context["rows"] = [
                [
                    line.submission.internal_id,
                    line.item.internal_id,
                    " ".join(part for part in [line.item.date_mm, line.item.denomination, line.item.series] if part),
                    line.declared_value or "",
                    timezone.localtime(line.created_at).strftime("%b %-d, %Y"),
                ]
                for line in queryset[:500]
            ]
        elif report_type == "sales":
            queryset = Sale.objects.annotate(item_count=Count("lines"), tube_count=Count("tube_lines")).order_by("-created_at")
            context["summary"] = [("Total sales", queryset.count())]
            context["headers"] = ["Sale", "Venue", "Items", "Tubes", "Created"]
            context["rows"] = [
                [
                    sale.internal_id,
                    sale.venue,
                    sale.item_count,
                    sale.tube_count,
                    timezone.localtime(sale.created_at).strftime("%b %-d, %Y"),
                ]
                for sale in queryset[:500]
            ]
        elif report_type == "incoming":
            _ensure_incoming_inventory_tables()
            queryset = IncomingInventoryBatch.objects.annotate(line_count=Count("lines")).order_by("-created_at")
            context["summary"] = [("Total incoming batches", queryset.count())]
            context["headers"] = ["Batch", "Vendor", "Invoice", "Status", "Rows", "Created"]
            context["rows"] = [
                [
                    batch.title or str(batch),
                    batch.vendor,
                    batch.invoice_number,
                    batch.get_parser_status_display(),
                    batch.line_count,
                    timezone.localtime(batch.created_at).strftime("%b %-d, %Y"),
                ]
                for batch in queryset[:500]
            ]
        elif report_type == "crackouts":
            queryset = CrackoutEvent.objects.select_related("item", "to_submission").order_by("-created_at")
            context["summary"] = [("Total crackout events", queryset.count())]
            context["headers"] = ["Item", "From Service", "From Grade", "To Submission", "Outcome", "Created"]
            context["rows"] = [
                [
                    event.item.internal_id,
                    event.from_service,
                    event.from_grade,
                    event.to_submission.internal_id if event.to_submission else "",
                    event.outcome,
                    timezone.localtime(event.created_at).strftime("%b %-d, %Y"),
                ]
                for event in queryset[:500]
            ]
        elif report_type == "locations":
            queryset = Location.objects.annotate(item_count=Count("items"), product_count=Count("products")).order_by("name")
            context["summary"] = [("Total locations", queryset.count())]
            context["headers"] = ["Location", "Inventory Items", "Products"]
            context["rows"] = [[location.name, location.item_count, location.product_count] for location in queryset[:500]]

        return render(request, self.report_template, context)


@admin.register(Container)
class ContainerAdmin(PortalBulkActionsMixin, admin.ModelAdmin):
    change_list_template = "admin/portalapp/container/change_list.html"
    list_display = ("internal_id","date_mm","denomination","series","label_text","quantity","ask_price","created_at")
    list_filter = (TubeSaleStatusListFilter,)
    search_fields = ("internal_id","date_mm","denomination","series","label_text","notes")
    fields = (
        "internal_id",
        "date_mm",
        "denomination",
        "series",
        "label_text",
        "quantity",
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
