from django.contrib import admin
from .models import (
    StudentProfile, TimetableEntry, Session, TopicSession,
    SlideDocument, CourseOutline, PastQuestion, CourseDefinition
)
from .outline_parser import _parse_course_outline
from .past_question_parser import _parse_past_question_file
from .slide_topic_extractor import _parse_slide_document
from .models import PreGeneratedLesson, SlideChunk

@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "level", "semester", "xp", "streak", "last_session_date"]
    search_fields = ["user__username", "user__first_name"]
    list_filter = ["level", "semester"]


@admin.register(TimetableEntry)
class TimetableEntryAdmin(admin.ModelAdmin):
    list_display = ["student", "course_code", "day", "time", "week_number", "is_completed", "is_missed"]
    list_filter = ["day", "is_completed", "is_missed"]
    search_fields = ["student__user__username", "course_code"]


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ["student", "course_code", "week_number", "current_topic_index", "is_complete", "xp_earned", "started_at"]
    list_filter = ["is_complete", "course_code"]
    search_fields = ["student__user__username", "course_code"]


@admin.register(TopicSession)
class TopicSessionAdmin(admin.ModelAdmin):
    list_display = ["session", "topic_name", "topic_index", "passed_quiz", "xp_earned", "is_complete"]
    list_filter = ["passed_quiz", "is_complete"]
    search_fields = ["session__student__user__username", "topic_name"]


@admin.register(SlideDocument)
class SlideDocumentAdmin(admin.ModelAdmin):
    list_display = ["course_code", "course_title", "level", "parsed", "uploaded_at"]
    list_filter = ["level", "parsed"]
    search_fields = ["course_code", "course_title"]
    readonly_fields = ["extracted_text", "extracted_topics", "parsed"]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        try:
            _parse_slide_document(obj)
            self.message_user(request, f"Extracted {len(obj.extracted_topics)} topics from slide.")
        except Exception as e:
            self.message_user(request, f"File saved but parsing failed: {str(e)}", level="warning")

@admin.register(SlideChunk)
class SlideChunkAdmin(admin.ModelAdmin):
    list_display = ["slide", "week_number", "chunk_preview"]
    list_filter = ["slide__course_code", "week_number"]
    search_fields = ["slide__course_code", "slide__course_title"]
    readonly_fields = ["slide", "week_number"]

    def chunk_preview(self, obj):
        return obj.chunk_text[:80] + "..." if len(obj.chunk_text) > 80 else obj.chunk_text
    chunk_preview.short_description = "Preview"


@admin.register(CourseOutline)
class CourseOutlineAdmin(admin.ModelAdmin):
    list_display = ["course_code", "course_title", "level", "parsed", "uploaded_at"]
    list_filter = ["level", "parsed"]
    search_fields = ["course_code", "course_title"]
    readonly_fields = ["extracted_text", "topics_json", "parsed"]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        try:
            _parse_course_outline(obj)
        except Exception as e:
            self.message_user(request, f"Outline saved but parsing failed: {str(e)}", level="warning")


@admin.register(PastQuestion)
class PastQuestionAdmin(admin.ModelAdmin):
    list_display = ["course_code", "course_title", "level", "parsed", "uploaded_at"]
    list_filter = ["level", "parsed"]
    search_fields = ["course_code", "course_title"]
    readonly_fields = ["extracted_text", "parsed_questions", "parsed"]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        try:
            _parse_past_question_file(obj)
            self.message_user(request, f"Parsed {len(obj.parsed_questions)} questions successfully.")
        except Exception as e:
            self.message_user(request, f"File saved but parsing failed: {str(e)}", level="warning")

@admin.register(CourseDefinition)
class CourseDefinitionAdmin(admin.ModelAdmin):
    list_display = ["course_code", "course_title", "level", "semester", "is_elective", "units"]
    list_filter = ["level", "semester", "is_elective", "school", "department"]
    search_fields = ["course_code", "course_title"]
    list_editable = ["is_elective"]

@admin.register(PreGeneratedLesson)
class PreGeneratedLessonAdmin(admin.ModelAdmin):
    list_display = ["course", "week_number", "topic_title", "is_published", "generated_at"]
    list_filter = ["course", "week_number", "is_published"]
    search_fields = ["course__course_code", "topic_title"]
    list_editable = ["is_published"]
    readonly_fields = ["generated_at", "updated_at"]