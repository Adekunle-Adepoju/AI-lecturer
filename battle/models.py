from django.db import models
from django.contrib.auth.models import User
from core.models import StudentProfile, SlideTopicChunk


LEVEL_CHOICES = [("300", "300 Level"), ("400", "400 Level")]

MODE_CHOICES = [
    ("alternating", "Alternating"),
    ("buzzer", "Buzzer"),
]

TEAM_CHOICES = [("A", "Team A"), ("B", "Team B")]

ROLE_CHOICES = [("player", "Player"), ("spectator", "Spectator")]

CONNECTION_CHOICES = [
    ("connected", "Connected"),
    ("disconnected", "Disconnected"),
]

ROOM_STATUS_CHOICES = [
    ("waiting", "Waiting"),
    ("in_progress", "In Progress"),
    ("finished", "Finished"),
]

QUESTION_STATUS_CHOICES = [
    ("pending_review", "Pending Review"),
    ("approved", "Approved"),
    ("retired", "Retired"),
]

QUESTION_TYPE_CHOICES = [
    ("static", "Static"),
    ("template", "Template"),
]

GENERATION_CHUNK_STATUS_CHOICES = [
    ("PENDING", "Pending"),
    ("COMPLETED", "Completed"),
    ("FAILED", "Failed"),
]


# ── Question bank ───────────────────────────────────────────────────

class BattleQuestion(models.Model):
    """A single MCQ in the battle question bank. Static questions carry
    their answer options in BattleAnswerOption; template questions carry
    no options here at all — see BattleQuestionTemplate, which computes
    values and distractors fresh at serve time."""
    course_code = models.CharField(max_length=10)
    level = models.CharField(max_length=3, choices=LEVEL_CHOICES)
    source_chunk = models.ForeignKey(
        SlideTopicChunk, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="battle_questions",
        help_text="Traceability back to the slide content this question was generated from.",
    )
    question_type = models.CharField(max_length=10, choices=QUESTION_TYPE_CHOICES, default="static")
    stem = models.TextField()
    status = models.CharField(max_length=15, choices=QUESTION_STATUS_CHOICES, default="pending_review")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["course_code", "-created_at"]
        indexes = [
            models.Index(fields=["course_code", "level", "status"]),
        ]

    def __str__(self):
        return f"{self.course_code} — {self.stem[:50]} ({self.status})"


class BattleAnswerOption(models.Model):
    """One candidate answer for a static BattleQuestion. Store 6-7 per
    question (over-stocked distractors) — 3 are picked at random at
    serve time, alongside the correct one, and their order is shuffled."""
    question = models.ForeignKey(BattleQuestion, on_delete=models.CASCADE, related_name="options")
    text = models.CharField(max_length=500)
    is_correct = models.BooleanField(default=False)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        marker = "✓" if self.is_correct else "✗"
        return f"{marker} {self.text[:40]}"


class BattleQuestionTemplate(models.Model):
    """A parameterized plug-and-solve question. Values are drawn at serve
    time within the stored ranges, the correct answer is computed from
    the formula, and distractors are produced by applying the named wrong
    operations — so one template serves effectively unlimited distinct
    questions with zero API calls and nothing to memorize verbatim."""
    question = models.OneToOneField(
        BattleQuestion, on_delete=models.CASCADE, related_name="template",
    )
    formula_description = models.TextField(
        help_text="Human-readable description of the governing equation, for staff review.",
    )
    variable_ranges = models.JSONField(
        default=dict,
        help_text='e.g. {"q": {"min": 100, "max": 2000, "unit": "STB/d"}, "dp": {"min": 50, "max": 500, "unit": "psi"}}',
    )
    correct_computation = models.CharField(
        max_length=100,
        help_text="Named rule the generation/serve code knows how to evaluate, e.g. 'productivity_index'.",
    )
    wrong_computations = models.JSONField(
        default=list,
        help_text="List of named wrong-operation rules to generate distractors from, e.g. ['invert_ratio', 'wrong_unit_conversion'].",
    )

    def __str__(self):
        return f"Template: {self.question.stem[:50]}"


class StudentQuestionExposure(models.Model):
    """Tracks how often and how recently a student has seen a given
    question (or template), so match setup can prefer unseen questions
    and prevent memorization from repeat play."""
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="battle_exposures")
    question = models.ForeignKey(BattleQuestion, on_delete=models.CASCADE, related_name="exposures")
    times_seen = models.PositiveIntegerField(default=0)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ["student", "question"]
        indexes = [
            models.Index(fields=["student", "last_seen_at"]),
        ]

    def __str__(self):
        return f"{self.student.user.username} — Q{self.question_id} (seen {self.times_seen}x)"


class BattleQuestionGenerationChunk(models.Model):
    """One macro-chunk of the offline question-generation run, tracked
    individually so a quota exhaustion or crash partway through a large
    batch doesn't lose already-generated questions — mirrors the
    SlideTopicSplitChunk pattern used for the lecture pipeline, adapted
    for a standalone manage.py command rather than an HTTP view."""
    source_chunk = models.ForeignKey(SlideTopicChunk, on_delete=models.CASCADE, related_name="battle_generation_chunks")
    status = models.CharField(max_length=10, choices=GENERATION_CHUNK_STATUS_CHOICES, default="PENDING")
    questions_generated = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["source_chunk"]

    def __str__(self):
        return f"Generation chunk for {self.source_chunk} ({self.status})"


# ── Rooms and matches ───────────────────────────────────────────────

class BattleRoom(models.Model):
    code = models.CharField(max_length=8, unique=True)
    leader = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="led_battle_rooms")
    mode = models.CharField(max_length=15, choices=MODE_CHOICES, null=True, blank=True)
    status = models.CharField(max_length=15, choices=ROOM_STATUS_CHOICES, default="waiting")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Room {self.code} ({self.status})"


class BattleParticipant(models.Model):
    room = models.ForeignKey(BattleRoom, on_delete=models.CASCADE, related_name="participants")
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="battle_participations")
    team = models.CharField(max_length=1, choices=TEAM_CHOICES, null=True, blank=True)
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default="player")
    connection_status = models.CharField(max_length=15, choices=CONNECTION_CHOICES, default="connected")
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["room", "student"]

    def __str__(self):
        return f"{self.student.user.username} in {self.room.code} ({self.role}{f', team {self.team}' if self.team else ''})"


class BattleMatch(models.Model):
    room = models.ForeignKey(BattleRoom, on_delete=models.CASCADE, related_name="matches")
    mode = models.CharField(max_length=15, choices=MODE_CHOICES)
    team_a_score = models.IntegerField(default=0)
    team_b_score = models.IntegerField(default=0)
    winner = models.CharField(max_length=1, choices=TEAM_CHOICES, null=True, blank=True)
    ended_by_disconnect = models.BooleanField(default=False)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Match in {self.room.code} — {self.team_a_score} : {self.team_b_score}"


class BattleMatchQuestion(models.Model):
    """One served question within a match — the audit trail. Records
    what was actually shown (options may be shuffled/subset for static
    questions, or freshly computed for template questions), so this is
    the source of truth for disputes, not the underlying BattleQuestion."""
    match = models.ForeignKey(BattleMatch, on_delete=models.CASCADE, related_name="match_questions")
    question = models.ForeignKey(BattleQuestion, on_delete=models.SET_NULL, null=True, related_name="match_appearances")
    sequence_number = models.PositiveIntegerField()
    served_stem = models.TextField()
    served_options_snapshot = models.JSONField(
        default=list,
        help_text="Exact options shown, in the exact order shown, with which index was correct.",
    )
    is_bonus = models.BooleanField(default=False)
    answering_team = models.CharField(max_length=1, choices=TEAM_CHOICES, null=True, blank=True)
    answering_student = models.ForeignKey(
        StudentProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="battle_answers",
    )
    answer_given_index = models.IntegerField(null=True, blank=True)
    correct = models.BooleanField(null=True, blank=True)
    points_awarded = models.IntegerField(default=0)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["match", "sequence_number"]
        unique_together = ["match", "sequence_number"]

    def __str__(self):
        return f"{self.match} — Q{self.sequence_number}"