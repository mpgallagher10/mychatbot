from django.urls import path

from . import views

urlpatterns = [
    path("turn-completed", views.turn_completed, name="turn_completed"),
]
