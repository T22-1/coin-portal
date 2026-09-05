from django.contrib import admin
from django.contrib import messages
from django.db import connection
from django.db.utils import DatabaseError
from django.db.models import OuterRef, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import Location, IncomingInventoryBatch, IncomingInventoryLine, InventoryItem, ItemPhoto, Certification, Submission, SubmissionItem, CrackoutEvent, Sale, SaleItem, SaleTube, Container, Report, _next_code
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


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    change_list_template = "admin/portalapp/report/change_list.html"

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

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["report_links"] = [
            {
                "title": "Inventory",
                "description": "All coin inventory with status, grading company, grade, cert, ask price, and location filters.",
                "url": reverse("admin:portalapp_inventoryitem_changelist"),
            },
            {
                "title": "Tubes",
                "description": "Tube inventory with in-stock and sold views.",
                "url": reverse("admin:portalapp_container_changelist"),
            },
            {
                "title": "Submissions",
                "description": "Submission packets by service and status.",
                "url": reverse("admin:portalapp_submission_changelist"),
            },
            {
                "title": "Submission Items",
                "description": "Coins attached to grading submissions with declared values.",
                "url": reverse("admin:portalapp_submissionitem_changelist"),
            },
            {
                "title": "Sales",
                "description": "Completed sales and invoice history.",
                "url": reverse("admin:portalapp_sale_changelist"),
            },
            {
                "title": "Incoming Inventory",
                "description": "Uploaded invoice batches and imported inventory rows.",
                "url": reverse("admin:portalapp_incominginventorybatch_changelist"),
            },
            {
                "title": "Crackout Events",
                "description": "Crackout workflow history and submission routing.",
                "url": reverse("admin:portalapp_crackoutevent_changelist"),
            },
            {
                "title": "Locations",
                "description": "Inventory location list for office, show, and storage tracking.",
                "url": reverse("admin:portalapp_location_changelist"),
            },
        ]
        extra_context["title"] = "Reports"
        context = {
            **self.admin_site.each_context(request),
            **extra_context,
            "opts": self.model._meta,
        }
        return render(request, self.change_list_template, context)


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
