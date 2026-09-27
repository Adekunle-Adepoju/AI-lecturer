# focus_sprint/views.py
import traceback

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
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


@csrf_exempt
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