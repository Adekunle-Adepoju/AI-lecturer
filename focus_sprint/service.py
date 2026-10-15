import re
import secrets
from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import models, transaction
from django.db.models import Q, Value
from django.db.models.functions import Lower, Replace
from django.utils import timezone

from core.departments import department_label
from core.models import StudentProfile
from .models import (
    FocusSprint, FocusSprintEvent, FocusRoom, RoomMember, RoomInvite,
    AWAY_BUDGET_SECONDS, MIN_DURATION_SECONDS,
    ROOM_MAX_MEMBERS, ROOM_GHOST_SECONDS, ROOM_JOIN_WINDOW_SECONDS,
)


class SprintError(Exception):
    pass


def _log(sprint, event_type, **payload):
    FocusSprintEvent.objects.create(sprint=sprint, event_type=event_type, payload=payload)


def _bypasses_duration_floor(profile):
    return profile.user.is_superuser or profile.is_staff_member


def start_sprint(profile, stake_xp, duration_seconds):
    if stake_xp <= 0:
        raise SprintError("invalid_stake")
    if not _bypasses_duration_floor(profile) and duration_seconds < MIN_DURATION_SECONDS:
        raise SprintError("below_minimum_duration")

    with transaction.atomic():
        student = type(profile).objects.select_for_update().get(pk=profile.pk)
        if FocusSprint.objects.filter(student=student, status="active").exists():
            raise SprintError("sprint_already_active")
        if student.xp < stake_xp:
            raise SprintError("insufficient_xp")

        student.xp -= stake_xp
        student.save(update_fields=["xp"])

        now = timezone.now()
        sprint = FocusSprint.objects.create(
            student=student, stake_initial=stake_xp, stake_current=stake_xp,
            started_at=now, target_duration_seconds=duration_seconds,
            target_ends_at=now + timedelta(seconds=duration_seconds),
            last_heartbeat_at=now,
        )
        _log(sprint, "started", stake=stake_xp, duration_seconds=duration_seconds)
    return sprint


def mark_away(sprint_id, student):
    with transaction.atomic():
        sprint = FocusSprint.objects.select_for_update().get(id=sprint_id, student=student, status="active")
        if sprint.awaiting_strike_decision:
            return _state(sprint)
        if sprint.away_started_at is None:
            sprint.away_started_at = timezone.now()
            sprint.save(update_fields=["away_started_at"])
            _log(sprint, "away_detected")
    return _state(sprint)


def heartbeat(sprint_id, student):
    """Resolves an away period if one was open, applies a strike if the away
    budget ran out, and checks for natural completion. Critically: any gap
    spent away EXTENDS target_ends_at by that same amount — the sprint clock
    only counts present time, so leaving the app pauses progress toward
    completion rather than burning down the target while you're gone."""
    now = timezone.now()
    with transaction.atomic():
        sprint = FocusSprint.objects.select_for_update().get(id=sprint_id, student=student, status="active")

        if sprint.awaiting_strike_decision:
            return _state(sprint)

        update_fields = {"last_heartbeat_at"}

        if sprint.away_started_at is not None:
            gap = max(0, int((now - sprint.away_started_at).total_seconds()))
            sprint.away_started_at = None
            sprint.away_budget_used_seconds += gap
            sprint.target_ends_at += timedelta(seconds=gap)  # pause — push the finish line back by the gap
            update_fields |= {"away_started_at", "away_budget_used_seconds", "target_ends_at"}
            _log(sprint, "returned", away_seconds=gap, budget_used=sprint.away_budget_used_seconds)

            if sprint.away_budget_used_seconds >= AWAY_BUDGET_SECONDS:
                _apply_strike(sprint)
                sprint.last_heartbeat_at = now
                update_fields |= {"awaiting_strike_decision", "stake_current", "strikes_count"}
                sprint.save(update_fields=list(update_fields))
                return _state(sprint)

        sprint.last_heartbeat_at = now
        sprint.save(update_fields=list(update_fields))

        if now >= sprint.target_ends_at:
            _complete(sprint)

    return _state(sprint)


def _apply_strike(sprint):
    sprint.stake_current = sprint.stake_current // 2
    sprint.strikes_count += 1
    sprint.away_budget_used_seconds = 0
    sprint.awaiting_strike_decision = True
    _log(sprint, "strike", strike_number=sprint.strikes_count, stake_now=sprint.stake_current)


def resolve_strike(sprint_id, student, continue_sprint):
    with transaction.atomic():
        sprint = FocusSprint.objects.select_for_update().get(id=sprint_id, student=student, status="active")
        if not sprint.awaiting_strike_decision:
            raise SprintError("no_strike_pending")

        if continue_sprint:
            sprint.awaiting_strike_decision = False
            sprint.away_started_at = None
            sprint.last_heartbeat_at = timezone.now()
            sprint.save(update_fields=["awaiting_strike_decision", "away_started_at", "last_heartbeat_at"])
            _log(sprint, "continued_after_strike", stake_now=sprint.stake_current)
        else:
            _payout(sprint, amount=sprint.stake_current, status="stopped_after_strike")

    return _state(sprint)


def stop_voluntarily(sprint_id, student):
    with transaction.atomic():
        sprint = FocusSprint.objects.select_for_update().get(id=sprint_id, student=student, status="active")
        if sprint.awaiting_strike_decision:
            raise SprintError("resolve_strike_first")
        _payout(sprint, amount=sprint.stake_current, status="stopped_voluntary")
    return _state(sprint)


def _complete(sprint):
    _payout(sprint, amount=sprint.stake_current * 2, status="completed")


def _payout(sprint, amount, status):
    student = sprint.student
    type(student).objects.filter(pk=student.pk).update(xp=models.F("xp") + amount)
    sprint.status = status
    sprint.ended_at = timezone.now()
    sprint.final_xp_awarded = amount - sprint.stake_initial
    sprint.save(update_fields=["status", "ended_at", "final_xp_awarded"])
    _log(sprint, status if status != "completed" else "completed", xp_awarded=amount)


def _state(sprint):
    now = timezone.now()
    paused_seconds = int((sprint.target_ends_at - sprint.started_at).total_seconds()) - sprint.target_duration_seconds
    return {
        "status": sprint.status,
        "awaiting_strike_decision": sprint.awaiting_strike_decision,
        "stake_initial": sprint.stake_initial,
        "stake_current": sprint.stake_current,
        "strikes_count": sprint.strikes_count,
        "started_at": sprint.started_at.isoformat(),
        "target_ends_at": sprint.target_ends_at.isoformat(),
        "paused_seconds": max(0, paused_seconds),
        "away_budget_remaining_seconds": (
            AWAY_BUDGET_SECONDS if sprint.away_started_at else
            max(0, AWAY_BUDGET_SECONDS - sprint.away_budget_used_seconds)
        ),
        "final_xp_awarded": sprint.final_xp_awarded,
    }

# ───────────────────────── Group rooms ─────────────────────────

def notify_room(code, message_type, **payload):
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(f"focus_room_{code}", {"type": message_type, **payload})
    except Exception:
        pass


def display_name(profile):
    u = profile.user
    return f"{u.first_name} {u.last_name}".strip() or u.username


def _new_room_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if not FocusRoom.objects.filter(code=code).exists():
            return code


def active_membership(profile):
    return (
        RoomMember.objects.select_related("room")
        .filter(student=profile, left_at__isnull=True, room__status="open")
        .first()
    )


def _depart(room, member, removed=False):
    """Caller holds the room lock. Marks the member gone, hands over the host
    role (longest-staying member), and closes the room when one person is left."""
    now = timezone.now()
    member.left_at = now
    member.removed = removed
    member.save(update_fields=["left_at", "removed"])
    rest = list(room.members.filter(left_at__isnull=True).select_related("student").order_by("joined_at", "id"))
    if len(rest) <= 1:
        for m in rest:
            m.left_at = now
            m.save(update_fields=["left_at"])
        room.status = "closed"
        room.save(update_fields=["status"])
        RoomInvite.objects.filter(room=room, status="pending").update(status="declined")
        return "closed"
    if room.host_id == member.student_id:
        room.host = rest[0].student
        room.save(update_fields=["host"])
        return "host_changed"
    return "left"


def create_room(profile):
    with transaction.atomic():
        if active_membership(profile):
            raise SprintError("already_in_room")
        room = FocusRoom.objects.create(code=_new_room_code(), host=profile)
        RoomMember.objects.create(room=room, student=profile)
    return room


def leave_room(profile):
    member = active_membership(profile)
    if member is None:
        raise SprintError("not_in_room")
    with transaction.atomic():
        room = FocusRoom.objects.select_for_update().get(pk=member.room_id)
        member = RoomMember.objects.filter(pk=member.pk, left_at__isnull=True).first()
        if member is None:
            raise SprintError("not_in_room")
        outcome = _depart(room, member)
    return room, outcome


def remove_member(host_profile, student_id):
    mine = active_membership(host_profile)
    if mine is None:
        raise SprintError("not_in_room")
    with transaction.atomic():
        room = FocusRoom.objects.select_for_update().get(pk=mine.room_id)
        if room.status != "open":
            raise SprintError("room_closed")
        if room.host_id != host_profile.id:
            raise SprintError("host_only")
        if student_id == host_profile.id:
            raise SprintError("cannot_remove_self")
        member = room.members.filter(student_id=student_id, left_at__isnull=True).first()
        if member is None:
            raise SprintError("not_a_member")
        outcome = _depart(room, member, removed=True)
    return room, outcome


def invite_student(profile, student_id):
    mine = active_membership(profile)
    if mine is None:
        raise SprintError("not_in_room")
    with transaction.atomic():
        room = FocusRoom.objects.select_for_update().get(pk=mine.room_id)
        if room.status != "open":
            raise SprintError("room_closed")
        target = StudentProfile.objects.select_related("user").filter(pk=student_id).first()
        if target is None:
            raise SprintError("student_not_found")
        if room.members.filter(student=target, left_at__isnull=True).exists():
            raise SprintError("already_member")
        if room.members.filter(left_at__isnull=True).count() >= ROOM_MAX_MEMBERS:
            raise SprintError("room_full")
        invite, _ = RoomInvite.objects.get_or_create(
            room=room, invited=target, status="pending", defaults={"invited_by": profile},
        )
    return room, invite


def accept_invite(profile, invite_id):
    with transaction.atomic():
        invite = RoomInvite.objects.select_for_update().filter(
            pk=invite_id, invited=profile, status="pending",
        ).first()
        if invite is None:
            raise SprintError("invite_not_found")
        room = FocusRoom.objects.select_for_update().get(pk=invite.room_id)
        if room.status != "open":
            raise SprintError("room_closed")
        if active_membership(profile):
            raise SprintError("already_in_room")
        if room.members.filter(left_at__isnull=True).count() >= ROOM_MAX_MEMBERS:
            raise SprintError("room_full")
        RoomMember.objects.create(room=room, student=profile)
        invite.status = "accepted"
        invite.save(update_fields=["status"])
    return room


def decline_invite(profile, invite_id):
    n = RoomInvite.objects.filter(pk=invite_id, invited=profile, status="pending").update(status="declined")
    if not n:
        raise SprintError("invite_not_found")


def pending_invites(profile):
    qs = RoomInvite.objects.filter(
        invited=profile, status="pending", room__status="open",
    ).select_related("room", "invited_by__user")
    return [{"id": i.id, "code": i.room.code, "by": display_name(i.invited_by)} for i in qs]


def search_students(profile, query):
    query = (query or "").strip()
    if len(query) < 3:
        return []
    squashed = re.sub(r"\s+", "", query).lower()
    name_q = Q()
    for word in query.split():
        name_q &= Q(user__first_name__icontains=word) | Q(user__last_name__icontains=word)
    qs = (
        StudentProfile.objects.select_related("user").exclude(pk=profile.pk)
        .annotate(matric_key=Replace(Lower("matric_number"), Value(" "), Value("")))
        .filter(Q(matric_key=squashed) | name_q)[:10]
    )
    return [
        {"id": p.id, "name": display_name(p), "department": department_label(p.department), "level": p.level}
        for p in qs
    ]


def drop_ghosts(room):
    """Drops members silent for ROOM_GHOST_SECONDS. Runs whenever room state is read."""
    cutoff = timezone.now() - timedelta(seconds=ROOM_GHOST_SECONDS)
    stale = list(room.members.filter(left_at__isnull=True, last_seen_at__lt=cutoff).values_list("pk", "student_id"))
    dropped, closed = [], False
    for pk, student_id in stale:
        with transaction.atomic():
            locked = FocusRoom.objects.select_for_update().get(pk=room.pk)
            member = RoomMember.objects.filter(pk=pk, left_at__isnull=True, last_seen_at__lt=cutoff).first()
            if locked.status != "open" or member is None:
                continue
            outcome = _depart(locked, member)
        dropped.append(student_id)
        if outcome == "closed":
            closed = True
            break
    for sid in dropped:
        notify_room(room.code, "room.kick", student_id=sid)
    if closed:
        notify_room(room.code, "room.closed")
    elif dropped:
        notify_room(room.code, "room.event", event={"type": "refresh"})


def start_round(profile, duration_seconds):
    mine = active_membership(profile)
    if mine is None:
        raise SprintError("not_in_room")
    if not _bypasses_duration_floor(profile) and duration_seconds < MIN_DURATION_SECONDS:
        raise SprintError("below_minimum_duration")
    with transaction.atomic():
        room = FocusRoom.objects.select_for_update().get(pk=mine.room_id)
        if room.status != "open":
            raise SprintError("room_closed")
        if room.host_id != profile.id:
            raise SprintError("host_only")
        if FocusSprint.objects.filter(room=room, status="active").exists():
            raise SprintError("round_in_progress")
        room.round_number += 1
        room.round_duration_seconds = duration_seconds
        room.round_started_at = timezone.now()
        room.save(update_fields=["round_number", "round_duration_seconds", "round_started_at"])
    return room


def join_round(profile, stake_xp):
    mine = active_membership(profile)
    if mine is None:
        raise SprintError("not_in_room")
    room = FocusRoom.objects.get(pk=mine.room_id)
    if room.status != "open" or room.round_started_at is None:
        raise SprintError("no_round")
    if (timezone.now() - room.round_started_at).total_seconds() > ROOM_JOIN_WINDOW_SECONDS:
        raise SprintError("join_window_closed")
    if FocusSprint.objects.filter(student=profile, room=room, started_at__gte=room.round_started_at).exists():
        raise SprintError("already_joined")
    with transaction.atomic():
        sprint = start_sprint(profile, stake_xp, room.round_duration_seconds)
        sprint.room = room
        sprint.save(update_fields=["room"])
    return sprint


def room_state(profile):
    """Room snapshot for the chat panel, or None if not in a room. Stakes are never included."""
    member = active_membership(profile)
    if member is None:
        return None
    RoomMember.objects.filter(pk=member.pk).update(last_seen_at=timezone.now())
    drop_ghosts(member.room)
    room = FocusRoom.objects.get(pk=member.room_id)
    if room.status != "open" or not room.members.filter(student=profile, left_at__isnull=True).exists():
        return None

    members = list(
        room.members.filter(left_at__isnull=True).select_related("student__user").order_by("joined_at", "id")
    )
    sprint_by_student = {}
    if room.round_started_at:
        for s in FocusSprint.objects.filter(room=room, started_at__gte=room.round_started_at).order_by("started_at"):
            sprint_by_student[s.student_id] = s

    def label(m):
        s = sprint_by_student.get(m.student_id)
        if s is None:
            return "waiting"
        if s.status == "active":
            return "away" if (s.away_started_at or s.awaiting_strike_decision) else "focusing"
        return "finished" if s.status == "completed" else "stopped"

    in_window = bool(room.round_started_at) and (
        (timezone.now() - room.round_started_at).total_seconds() <= ROOM_JOIN_WINDOW_SECONDS
    )
    can_join = (
        in_window
        and profile.id not in sprint_by_student
        and not FocusSprint.objects.filter(student=profile, status="active").exists()
    )
    return {
        "code": room.code,
        "me": profile.id,
        "host_id": room.host_id,
        "max_members": ROOM_MAX_MEMBERS,
        "round_number": room.round_number,
        "round_duration_seconds": room.round_duration_seconds,
        "round_live": FocusSprint.objects.filter(room=room, status="active").exists(),
        "can_join_round": can_join,
        "members": [
            {"student_id": m.student_id, "name": display_name(m.student), "status": label(m)}
            for m in members
        ],
    }