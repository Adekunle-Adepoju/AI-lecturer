from django.urls import path
from . import views

app_name = "engagement"
urlpatterns = [
    path("", views.ingest, name="ingest"),
    path("dashboard/", views.dashboard, name="dashboard"),
]