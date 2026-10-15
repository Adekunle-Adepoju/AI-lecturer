# focus_sprint/views.py
import traceback

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from functools import wraps
from django.views.decorators.http import require_POST, require_GET
from django.shortcuts import get_object_or_404

from .models import FocusSprint
from . import service


@login_required
@require_POST
def start_view(request):
    try:
        sprint = service.start_sprint(
            request.user.profile,
            stake_xp=int(request.POST.get("stake_xp", 0)),
            duration_seconds=int(request.POST.get("duration_seconds", 0)),
        )
        return JsonResponse({"sprint_id": sprint.id, **service._state(sprint)})
    except service.SprintError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except (ValueError, TypeError):
        return JsonResponse({"error": "invalid_input"}, status=400)
    except Exception:
        traceback.print_exc()
        return JsonResponse({"error": "server_error"}, status=500)


@login_required
@require_POST
def heartbeat_view(request, sprint_id):
    try:
        return JsonResponse(service.heartbeat(sprint_id, request.user.profile))
    except FocusSprint.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    except service.SprintError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception:
        traceback.print_exc()
        return JsonResponse({"error": "server_error"}, status=500)


@login_required
@require_POST
def away_view(request, sprint_id):
    try:
        service.mark_away(sprint_id, request.user.profile)
    except Exception:
        traceback.print_exc()
    return HttpResponse(status=204)


@login_required
@require_POST
def resolve_strike_view(request, sprint_id):
    try:
        state = service.resolve_strike(
            sprint_id, request.user.profile,
            continue_sprint=request.POST.get("continue") == "true",
        )
        return JsonResponse(state)
    except FocusSprint.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    except service.SprintError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception:
        traceback.print_exc()
        return JsonResponse({"error": "server_error"}, status=500)


@login_required
@require_POST
def stop_view(request, sprint_id):
    try:
        state = service.stop_voluntarily(sprint_id, request.user.profile)
        return JsonResponse(state)
    except FocusSprint.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    except service.SprintError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception:
        traceback.print_exc()
        return JsonResponse({"error": "server_error"}, status=500)


@login_required
def state_view(request, sprint_id):
    sprint = get_object_or_404(FocusSprint, id=sprint_id, student=request.user.profile)
    return JsonResponse(service._state(sprint))

# ───────────────────────── Group rooms ─────────────────────────

def _api(fn):
    @wraps(fn)
    def wrapper(request, *args, **kwargs):
        profile = getattr(request.user, "profile", None)
        if profile is None:
            return JsonResponse({"error": "no_profile"}, status=400)
        try:
            return fn(request, profile, *args, **kwargs)
        except service.SprintError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except (ValueError, TypeError):
            return JsonResponse({"error": "invalid_input"}, status=400)
        except Exception:
            traceback.print_exc()
            return JsonResponse({"error": "server_error"}, status=500)
    return wrapper


def _refresh_nudge(code):
    service.notify_room(code, "room.event", event={"type": "refresh"})


def _after_departure(room, outcome, student_id):
    service.notify_room(room.code, "room.kick", student_id=student_id)
    if outcome == "closed":
        service.notify_room(room.code, "room.closed")
    else:
        _refresh_nudge(room.code)


@login_required
@require_POST
@_api
def room_create_view(request, profile):
    room = service.create_room(profile)
    return JsonResponse({"code": room.code})


@login_required
@require_GET
@_api
def room_state_view(request, profile):
    state = service.room_state(profile)
    if state is None:
        return JsonResponse({"room": None, "invites": service.pending_invites(profile)})
    return JsonResponse({"room": state})


@login_required
@require_POST
@_api
def room_leave_view(request, profile):
    room, outcome = service.leave_room(profile)
    _after_departure(room, outcome, profile.id)
    return JsonResponse({"ok": True})


@login_required
@require_POST
@_api
def room_remove_view(request, profile):
    target_id = int(request.POST.get("student_id", 0))
    room, outcome = service.remove_member(profile, target_id)
    _after_departure(room, outcome, target_id)
    return JsonResponse({"ok": True})


@login_required
@require_GET
@_api
def room_search_view(request, profile):
    if service.active_membership(profile) is None:
        raise service.SprintError("not_in_room")
    return JsonResponse({"results": service.search_students(profile, request.GET.get("q", ""))})


@login_required
@require_POST
@_api
def room_invite_view(request, profile):
    room, invite = service.invite_student(profile, int(request.POST.get("student_id", 0)))
    return JsonResponse({"ok": True, "name": service.display_name(invite.invited)})


@login_required
@require_POST
@_api
def room_accept_view(request, profile, invite_id):
    room = service.accept_invite(profile, invite_id)
    _refresh_nudge(room.code)
    return JsonResponse({"ok": True})


@login_required
@require_POST
@_api
def room_decline_view(request, profile, invite_id):
    service.decline_invite(profile, invite_id)
    return JsonResponse({"ok": True})


@login_required
@require_POST
@_api
def room_round_start_view(request, profile):
    room = service.start_round(profile, int(request.POST.get("duration_seconds", 0)))
    _refresh_nudge(room.code)
    return JsonResponse({"ok": True})


@login_required
@require_POST
@_api
def room_round_join_view(request, profile):
    sprint = service.join_round(profile, int(request.POST.get("stake_xp", 0)))
    _refresh_nudge(sprint.room.code)
    return JsonResponse({"ok": True, "sprint_id": sprint.id})