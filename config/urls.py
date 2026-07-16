from django.contrib import admin
from django.urls import include, path

from turns import views as turn_views

urlpatterns = [
    path("healthz", turn_views.healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("webhooks/", include("turns.urls")),
]
