# focus_sprint/admin.py
from django.contrib import admin
from .models import FocusSprint, FocusSprintEvent


class FocusSprintEventInline(admin.TabularInline):
    model = FocusSprintEvent
    extra = 0
    readonly_fields = ["event_type", "payload", "created_at"]
    can_delete = False
    ordering = ["created_at"]


@admin.register(FocusSprint)
class FocusSprintAdmin(admin.ModelAdmin):
    list_display = ["student", "status", "stake_initial", "stake_current", "strikes_count", "started_at", "ended_at"]
    list_filter = ["status"]
    search_fields = ["student__user__username"]
    readonly_fields = [f.name for f in FocusSprint._meta.fields]
    inlines = [FocusSprintEventInline]


@admin.register(FocusSprintEvent)
class FocusSprintEventAdmin(admin.ModelAdmin):
    list_display = ["sprint", "event_type", "created_at"]
    list_filter = ["event_type"]
    readonly_fields = ["sprint", "event_type", "payload", "created_at"]