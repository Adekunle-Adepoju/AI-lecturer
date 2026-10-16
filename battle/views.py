import random
import string

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404

from .models import BattleRoom
from .landing import landing_context
from .models import BattleRoom, BattleStats
from .tiers import tier_for
import time
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from .consumers import push_to_user, PRESENCE_KEY, PRESENCE_TTL


def _generate_room_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


@login_required
def create_room_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")
    profile = request.user.profile

    if request.method == "POST":
        code = _generate_room_code()
        while BattleRoom.objects.filter(code=code).exists():
            code = _generate_room_code()
        room = BattleRoom.objects.create(
            code=code, leader=profile, is_public=bool(request.POST.get("public")),
        )
        return redirect("battle_room_lobby", room_code=room.code)

    stats = BattleStats.objects.filter(student=profile).first()
    ctx = landing_context(request)
    ctx.update({"battle_stats": stats, "tier": tier_for(stats.wins) if stats else None})
    return render(request, "battle/create_room.html", ctx)


@login_required
def join_room_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    error = None
    if request.method == "POST":
        code = request.POST.get("code", "").strip().upper()
        room = BattleRoom.objects.filter(code=code).first()
        if room is None:
            error = "No room with that code."
        elif room.status == "finished":
            error = "That match has already finished."
        else:
            return redirect("battle_room_lobby", room_code=room.code)

    return render(request, "battle/join_room.html", {"error": error})


@login_required
def room_lobby_view(request, room_code):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    room = get_object_or_404(BattleRoom, code=room_code)
    return render(request, "battle/room_lobby.html", {
        "room_code": room.code,
        "student_id": request.user.profile.id,
    })

@login_required
def online_players_view(request):
    from .landing import _active_players
    data = _active_players(limit=50, exclude_id=request.user.id)
    return JsonResponse({"players": data["players"]})

@login_required
@require_POST
def invite_online_view(request):
    profile = getattr(request.user, "profile", None)
    if profile is None:
        return JsonResponse({"error": "invalid"}, status=400)
    room = BattleRoom.objects.filter(leader=profile, status="waiting").order_by("-id").first()
    if room is None:
        return JsonResponse({"error": "no_room"}, status=400)
    try:
        target_id = int(request.POST.get("user_id", 0))
    except (TypeError, ValueError):
        return JsonResponse({"error": "invalid"}, status=400)
    if target_id == request.user.id:
        return JsonResponse({"error": "invalid"}, status=400)

    seen = (cache.get(PRESENCE_KEY) or {}).get(target_id)
    if not seen or time.time() - seen[0] > PRESENCE_TTL:
        return JsonResponse({"error": "not_online"}, status=400)

    key = f"battleinv:{request.user.id}:{target_id}"
    if cache.get(key):
        return JsonResponse({"error": "cooldown"}, status=400)
    cache.set(key, 1, 60)

    name = request.user.first_name or request.user.username
    push_to_user(target_id, {"type": "battle_invite", "by": name, "code": room.code})
    return JsonResponse({"ok": True})

@login_required
@require_POST
def quick_match_view(request):
    profile = getattr(request.user, "profile", None)
    if profile is None:
        return redirect("onboarding")
    mine = BattleRoom.objects.filter(leader=profile, status="waiting").order_by("-id").first()
    if mine:
        return redirect("battle_room_lobby", room_code=mine.code)
    candidates = (BattleRoom.objects.filter(is_public=True, status="waiting",
                                            participants__connection_status="connected")
                  .distinct().order_by("-id")[:20])
    for room in candidates:
        if room.participants.filter(role="player").count() < 10:
            return redirect("battle_room_lobby", room_code=room.code)
    code = _generate_room_code()
    while BattleRoom.objects.filter(code=code).exists():
        code = _generate_room_code()
    room = BattleRoom.objects.create(code=code, leader=profile, is_public=True)
    return redirect("battle_room_lobby", room_code=room.code)