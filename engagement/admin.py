from django.contrib import admin
from .models import DailyActivity, ModuleDaily

@admin.register(DailyActivity)
class DailyActivityAdmin(admin.ModelAdmin):
    list_display = ("date", "student", "active_seconds", "tts_seconds")
    list_filter = ("date",)

@admin.register(ModuleDaily)
class ModuleDailyAdmin(admin.ModelAdmin):
    list_display = ("date", "student", "course_code", "topic_name", "read_seconds", "tts_seconds")
    list_filter = ("course_code", "date")