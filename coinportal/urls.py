from django.contrib import admin
from django.urls import path, include
from portalapp import views as pviews

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", pviews.login_view, name="login"),
    path("logout/", pviews.logout_view, name="logout"),
    path("", include("portalapp.urls")),
]
