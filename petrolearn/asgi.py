"""
ASGI config for petrolearn project.
"""

import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'petrolearn.settings')

import django
django.setup()

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from django.core.asgi import get_asgi_application

django_asgi_app = get_asgi_application()

from battle.routing import websocket_urlpatterns

import traceback

class LogWS:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        print("WS reached app:", scope.get("path"), flush=True)
        try:
            return await self.app(scope, receive, send)
        except Exception:
            traceback.print_exc()
            raise

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": LogWS(AuthMiddlewareStack(URLRouter(websocket_urlpatterns))),
})

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AuthMiddlewareStack(
        URLRouter(websocket_urlpatterns)
    ),
})