from django.contrib import admin
from .models import (
    BattleQuestion, BattleAnswerOption, BattleQuestionTemplate,
    StudentQuestionExposure, BattleQuestionGenerationChunk,
    BattleRoom, BattleParticipant, BattleMatch, BattleMatchQuestion,
)


class BattleAnswerOptionInline(admin.TabularInline):
    model = BattleAnswerOption
    extra = 0


@admin.register(BattleQuestion)
class BattleQuestionAdmin(admin.ModelAdmin):
    list_display = ["stem_preview", "course_code", "level", "question_type", "status", "created_at"]
    list_filter = ["status", "course_code", "level", "question_type"]
    search_fields = ["stem"]
    inlines = [BattleAnswerOptionInline]
    actions = ["approve_questions", "retire_questions"]

    def stem_preview(self, obj):
        return obj.stem[:70]
    stem_preview.short_description = "Question"

    @admin.action(description="Approve selected questions")
    def approve_questions(self, request, queryset):
        updated = queryset.update(status="approved")
        self.message_user(request, f"{updated} question(s) approved.")

    @admin.action(description="Retire selected questions")
    def retire_questions(self, request, queryset):
        updated = queryset.update(status="retired")
        self.message_user(request, f"{updated} question(s) retired.")


@admin.register(BattleQuestionGenerationChunk)
class BattleQuestionGenerationChunkAdmin(admin.ModelAdmin):
    list_display = ["source_chunk", "status", "questions_generated", "updated_at"]
    list_filter = ["status"]


@admin.register(BattleQuestionTemplate)
class BattleQuestionTemplateAdmin(admin.ModelAdmin):
    list_display = ["question", "correct_computation"]


@admin.register(StudentQuestionExposure)
class StudentQuestionExposureAdmin(admin.ModelAdmin):
    list_display = ["student", "question", "times_seen", "last_seen_at"]
    list_filter = ["last_seen_at"]


@admin.register(BattleRoom)
class BattleRoomAdmin(admin.ModelAdmin):
    list_display = ["code", "leader", "mode", "status", "created_at"]
    list_filter = ["status", "mode"]


@admin.register(BattleParticipant)
class BattleParticipantAdmin(admin.ModelAdmin):
    list_display = ["student", "room", "team", "role", "connection_status"]
    list_filter = ["team", "role", "connection_status"]


class BattleMatchQuestionInline(admin.TabularInline):
    model = BattleMatchQuestion
    extra = 0
    readonly_fields = ["question", "sequence_number", "served_stem", "served_options_snapshot",
                        "is_bonus", "answering_team", "answering_student", "answer_given_index",
                        "correct", "points_awarded", "resolved_at"]
    can_delete = False


@admin.register(BattleMatch)
class BattleMatchAdmin(admin.ModelAdmin):
    list_display = ["room", "mode", "team_a_score", "team_b_score", "winner", "ended_by_disconnect", "started_at"]
    list_filter = ["mode", "winner", "ended_by_disconnect"]
    inlines = [BattleMatchQuestionInline]