from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r"ws/battle/(?P<room_code>\w+)/$", consumers.BattleConsumer.as_asgi()),
]