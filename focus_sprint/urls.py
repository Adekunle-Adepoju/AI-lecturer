# focus_sprint/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path("start/", views.start_view, name="focus_sprint_start"),
    path("<int:sprint_id>/heartbeat/", views.heartbeat_view, name="focus_sprint_heartbeat"),
    path("<int:sprint_id>/away/", views.away_view, name="focus_sprint_away"),
    path("<int:sprint_id>/resolve-strike/", views.resolve_strike_view, name="focus_sprint_resolve_strike"),
    path("<int:sprint_id>/stop/", views.stop_view, name="focus_sprint_stop"),
    path("<int:sprint_id>/state/", views.state_view, name="focus_sprint_state"),
]