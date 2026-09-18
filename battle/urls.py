from django.urls import path
from . import views

urlpatterns = [
    path("create/", views.create_room_view, name="battle_create_room"),
    path("join/", views.join_room_view, name="battle_join_room"),
    path("room/<str:room_code>/", views.room_lobby_view, name="battle_room_lobby"),
]