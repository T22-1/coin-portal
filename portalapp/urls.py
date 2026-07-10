from django.urls import path
from . import views

urlpatterns = [
    path("", views.pricing, name="home"),
    path("dashboard/", views.home, name="dashboard"),
    path("pricing/", views.pricing, name="pricing"),
    path("inventory/", views.inventory_master_list, name="inventory_master_list"),
    path("submissions/active/", views.active_submissions, name="active_submissions"),
    path("i/<str:code>/", views.item_by_code, name="item_by_code"),
    path("t/<str:code>/", views.tube_by_code, name="tube_by_code"),

    path("scan/", views.scan, name="scan"),
    path("sale/start/", views.sale_start, name="sale_start"),
    path("sale/add/", views.sale_add_scan, name="sale_add_scan"),
    path("sale/", views.sale_batch, name="sale_batch"),
    path("sale/complete/", views.sale_complete, name="sale_complete"),

    path("labels/item/<str:code>.pdf", views.label_item_pdf, name="label_item_pdf"),
    path("labels/tube/<str:code>.pdf", views.label_tube_pdf, name="label_tube_pdf"),

    path("submissions/<int:submission_id>/", views.submission_packet, name="submission_packet"),
    path("submissions/<int:submission_id>/add-scan/", views.submission_add_scan, name="submission_add_scan"),
    path("submissions/<int:submission_id>/remove/<int:line_id>/", views.submission_remove_item, name="submission_remove_item"),
    path("submissions/<int:submission_id>/packet.csv", views.submission_packet_csv, name="submission_packet_csv"),
    path("submissions/<int:submission_id>/packet.pdf", views.submission_packet_pdf, name="submission_packet_pdf"),
    path("submissions/<int:submission_id>/pcgs.pdf", views.submission_pcgs_pdf, name="submission_pcgs_pdf"),
    path("submissions/<int:submission_id>/ngc.pdf", views.submission_ngc_pdf, name="submission_ngc_pdf"),
    path("submissions/<int:submission_id>/cac.pdf", views.submission_cac_pdf, name="submission_cac_pdf"),
    path("submissions/<int:submission_id>/cacg.pdf", views.submission_cacg_pdf, name="submission_cacg_pdf"),
]
