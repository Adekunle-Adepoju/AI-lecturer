from django.urls import path
from . import views

urlpatterns = [
    path("create/", views.create_room_view, name="battle_create_room"),
    path("join/", views.join_room_view, name="battle_join_room"),
    path("room/<str:room_code>/", views.room_lobby_view, name="battle_room_lobby"),
    path("online-players/", views.online_players_view, name="battle_online_players"),
    path("invite-online/", views.invite_online_view, name="battle_invite_online"),
    path("quick/", views.quick_match_view, name="battle_quick_match"),
]