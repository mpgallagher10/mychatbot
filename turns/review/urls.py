from django.urls import path

from . import views

app_name = "review"

urlpatterns = [
    path("<str:token>", views.overview, name="overview"),
    path("<str:token>/finding/<int:finding_id>", views.finding, name="finding"),
    path("<str:token>/finding/<int:finding_id>/act", views.act, name="act"),
    path("<str:token>/photo/<int:photo_id>", views.photo, name="photo"),
    path("<str:token>/summary", views.summary, name="summary"),
    path("<str:token>/finalize", views.finalize, name="finalize"),
]
