from django.contrib import admin
from django.contrib import messages
from django.db import connection
from django.db.models import OuterRef, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import Location, InventoryItem, ItemPhoto, Certification, Submission, SubmissionItem, CrackoutEvent, Sale, SaleItem, Container, _next_code
from .views import item_labels_pdf_response

@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    search_fields = ("name",)

class PhotoInline(admin.TabularInline):
    model = ItemPhoto
    extra = 0

class CertInline(admin.TabularInline):
    model = Certification
    extra = 0

@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
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
    list_filter = ("holder", "status", "cac_sticker", "cacg_holder", "location")
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
    inlines = [PhotoInline, CertInline]

    class Media:
        js = ("portalapp/admin_print_labels.js",)

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
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ("internal_id","packet_link","service","status","created_at")
    list_filter = ("service","status")
    search_fields = ("internal_id","notes")
    fields = ("internal_id", "service", "status", "notes")

    class Media:
        js = ("portalapp/admin_submission_packet.js",)

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

@admin.register(SubmissionItem)
class SubmissionItemAdmin(admin.ModelAdmin):
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
class CrackoutAdmin(admin.ModelAdmin):
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
class SaleAdmin(admin.ModelAdmin):
    list_display = ("internal_id","venue","created_at")
    search_fields = ("internal_id","venue","notes")

@admin.register(SaleItem)
class SaleItemAdmin(admin.ModelAdmin):
    list_display = ("sale","item","sold_price")
    search_fields = ("sale__internal_id","item__internal_id")

@admin.register(Container)
class ContainerAdmin(admin.ModelAdmin):
    list_display = ("internal_id","label_text","quantity","ask_price","created_at")
    search_fields = ("internal_id","label_text","notes")
from django.contrib import admin

admin.site.site_header = "CoinPortal 365 Administration"
admin.site.site_title = "CoinPortal 365"
admin.site.index_title = "CoinPortal Management"
