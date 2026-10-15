from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r"ws/battle/(?P<room_code>\w+)/$", consumers.BattleConsumer.as_asgi()),
    re_path(r"ws/focus-room/(?P<code>\w+)/$", consumers.FocusRoomConsumer.as_asgi()),
    re_path(r"ws/notify/$", consumers.UserNotifyConsumer.as_asgi()),
]