# focus_sprint/service.py
from django.db import models, transaction
from django.utils import timezone
from datetime import timedelta

from .models import FocusSprint, FocusSprintEvent, AWAY_BUDGET_SECONDS, MIN_DURATION_SECONDS


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