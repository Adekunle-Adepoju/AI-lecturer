from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    path("", include("core.urls")),
    path("battle/", include("battle.urls")),
    path("pulse/", include("engagement.urls")),
    path("focus-sprint/", include("focus_sprint.urls"))
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)