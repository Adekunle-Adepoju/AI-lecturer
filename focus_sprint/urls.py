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
    path("room/create/", views.room_create_view, name="focus_room_create"),
    path("room/state/", views.room_state_view, name="focus_room_state"),
    path("room/leave/", views.room_leave_view, name="focus_room_leave"),
    path("room/remove/", views.room_remove_view, name="focus_room_remove"),
    path("room/search/", views.room_search_view, name="focus_room_search"),
    path("room/invite/", views.room_invite_view, name="focus_room_invite"),
    path("room/invites/<int:invite_id>/accept/", views.room_accept_view, name="focus_room_accept"),
    path("room/invites/<int:invite_id>/decline/", views.room_decline_view, name="focus_room_decline"),
    path("room/round/start/", views.room_round_start_view, name="focus_room_round_start"),
    path("room/round/join/", views.room_round_join_view, name="focus_room_round_join"),
    path("room/join/", views.room_join_view, name="focus_room_join"),
]