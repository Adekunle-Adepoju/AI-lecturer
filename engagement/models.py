from django.db import models
from django.utils import timezone


class _Base(models.Model):
    student = models.ForeignKey(
        "core.StudentProfile", on_delete=models.CASCADE, related_name="%(class)s_rows"
    )
    date = models.DateField()                     # Africa/Lagos calendar day
    tts_seconds = models.PositiveIntegerField(default=0)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        abstract = True


class DailyActivity(_Base):
    """One row per student per day. Row exists = student opened Rovea that day.
    'Engaged' is decided at QUERY time from these raw seconds (see reports.py)."""
    active_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["student", "date"], name="uniq_daily_activity")]
        indexes = [models.Index(fields=["date"])]


class ModuleDaily(_Base):
    """Per student, per day, per module (topic). Denormalised on purpose so it
    survives Session/TopicSession deletion (restart / staff reset)."""
    course_code = models.CharField(max_length=10)
    week_number = models.PositiveSmallIntegerField()
    topic_name = models.CharField(max_length=200)
    read_seconds = models.PositiveIntegerField(default=0)
    tts_plays = models.PositiveIntegerField(default=0)
    tts_completions = models.PositiveIntegerField(default=0)
    parts_viewed = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["student", "date", "course_code", "week_number", "topic_name"],
            name="uniq_module_daily")]
        indexes = [models.Index(fields=["course_code", "week_number", "topic_name"]),
                   models.Index(fields=["date"])]