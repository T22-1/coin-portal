from django.contrib import admin
from .models import Location, InventoryItem, ItemPhoto, Certification, Submission, SubmissionItem, CrackoutEvent, Sale, SaleItem, Container

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
    list_display = ("internal_id","date_mm","series","holder","grade_text","ask_price","status","location")
    list_filter = ("holder","status","cac_sticker","cacg_holder")
    search_fields = ("internal_id","date_mm","series","cert_number","variety","notes")
    inlines = [PhotoInline, CertInline]

@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ("internal_id","service","status","created_at")
    list_filter = ("service","status")
    search_fields = ("internal_id","notes")

@admin.register(SubmissionItem)
class SubmissionItemAdmin(admin.ModelAdmin):
    list_display = ("submission","item","declared_value","created_at")
    search_fields = ("submission__internal_id","item__internal_id")

@admin.register(CrackoutEvent)
class CrackoutAdmin(admin.ModelAdmin):
    list_display = ("item","from_service","from_grade","outcome","created_at")
    search_fields = ("item__internal_id","from_cert","reason","outcome")

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
