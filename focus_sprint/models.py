# focus_sprint/models.py
from django.db import models
from django.utils import timezone
from core.models import StudentProfile

AWAY_BUDGET_SECONDS = 10 * 60
MIN_DURATION_SECONDS = 2 * 60 * 60  # non-admin floor
ROOM_MAX_MEMBERS = 10
ROOM_GHOST_SECONDS = 10 * 60        # silent this long -> dropped from the room
ROOM_JOIN_WINDOW_SECONDS = 10 * 60  # how long after the host starts a round people can still join


class FocusSprint(models.Model):
    STATUS = [
        ("active", "Active"),
        ("completed", "Completed — doubled"),
        ("stopped_voluntary", "Stopped by student — no strikes"),
        ("stopped_after_strike", "Stopped by student — after a strike"),
    ]

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="focus_sprints")
    status = models.CharField(max_length=25, choices=STATUS, default="active")

    stake_initial = models.PositiveIntegerField()
    stake_current = models.PositiveIntegerField()

    started_at = models.DateTimeField(default=timezone.now)
    target_duration_seconds = models.PositiveIntegerField()
    target_ends_at = models.DateTimeField()

    away_started_at = models.DateTimeField(null=True, blank=True)
    away_budget_used_seconds = models.PositiveIntegerField(default=0)
    strikes_count = models.PositiveSmallIntegerField(default=0)

    awaiting_strike_decision = models.BooleanField(default=False)

    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    final_xp_awarded = models.IntegerField(null=True, blank=True)
    room = models.ForeignKey(
        "FocusRoom", null=True, blank=True, on_delete=models.SET_NULL, related_name="sprints",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student"], condition=models.Q(status="active"),
                name="uniq_active_focus_sprint_per_student",
            )
        ]
        indexes = [models.Index(fields=["student", "status"])]

    def __str__(self):
        return f"{self.student.user.username} — {self.status} ({self.stake_current} XP)"

    @property
    def away_budget_remaining_seconds(self):
        """Mirrors service._state()'s computation exactly — kept here too
        because base.html reads this straight off the model instance via
        the context processor, without going through the service layer."""
        if self.away_started_at:
            return AWAY_BUDGET_SECONDS
        return max(0, AWAY_BUDGET_SECONDS - self.away_budget_used_seconds)
    @property
    def paused_seconds(self):
        val = int((self.target_ends_at - self.started_at).total_seconds()) - self.target_duration_seconds
        return max(0, val)


class FocusSprintEvent(models.Model):
    EVENTS = [
        ("started", "Started"), ("away_detected", "Went away"), ("returned", "Returned"),
        ("strike", "Strike — stake halved"), ("continued_after_strike", "Continued after strike"),
        ("stopped_voluntary", "Stopped voluntarily"), ("stopped_after_strike", "Stopped after strike"),
        ("completed", "Completed — doubled"),
    ]
    sprint = models.ForeignKey(FocusSprint, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=25, choices=EVENTS)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.sprint_id} — {self.event_type}"

class FocusRoom(models.Model):
    STATUS = [("open", "Open"), ("closed", "Closed")]

    code = models.CharField(max_length=8, unique=True)
    host = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="hosted_focus_rooms")
    status = models.CharField(max_length=10, choices=STATUS, default="open")
    round_number = models.PositiveIntegerField(default=0)
    round_duration_seconds = models.PositiveIntegerField(default=0)
    round_started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Room {self.code} ({self.status})"


class RoomMember(models.Model):
    room = models.ForeignKey(FocusRoom, on_delete=models.CASCADE, related_name="members")
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="focus_room_memberships")
    joined_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    left_at = models.DateTimeField(null=True, blank=True)
    removed = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student"], condition=models.Q(left_at__isnull=True),
                name="uniq_active_room_per_student",
            )
        ]
        indexes = [models.Index(fields=["room", "left_at"])]


class RoomInvite(models.Model):
    STATUS = [("pending", "Pending"), ("accepted", "Accepted"), ("declined", "Declined")]

    room = models.ForeignKey(FocusRoom, on_delete=models.CASCADE, related_name="invites")
    invited = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="focus_room_invites")
    invited_by = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="focus_room_invites_sent")
    status = models.CharField(max_length=10, choices=STATUS, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["room", "invited"], condition=models.Q(status="pending"),
                name="uniq_pending_invite_per_room_student",
            )
        ]