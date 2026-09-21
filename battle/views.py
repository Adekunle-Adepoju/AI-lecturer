import random
import string

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404

from .models import BattleRoom


def _generate_room_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


@login_required
def create_room_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    if request.method == "POST":
        code = _generate_room_code()
        while BattleRoom.objects.filter(code=code).exists():
            code = _generate_room_code()
        room = BattleRoom.objects.create(code=code, leader=request.user.profile)
        return redirect("battle_room_lobby", room_code=room.code)

    return render(request, "battle/create_room.html")


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

