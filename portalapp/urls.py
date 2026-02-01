from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("i/<str:code>/", views.item_by_code, name="item_by_code"),
    path("t/<str:code>/", views.tube_by_code, name="tube_by_code"),

    path("scan/", views.scan, name="scan"),
    path("sale/start/", views.sale_start, name="sale_start"),
    path("sale/add/", views.sale_add_scan, name="sale_add_scan"),
    path("sale/", views.sale_batch, name="sale_batch"),
    path("sale/complete/", views.sale_complete, name="sale_complete"),

    path("labels/item/<str:code>.pdf", views.label_item_pdf, name="label_item_pdf"),
    path("labels/tube/<str:code>.pdf", views.label_tube_pdf, name="label_tube_pdf"),
]
