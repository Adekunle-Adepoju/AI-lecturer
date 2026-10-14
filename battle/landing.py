from zoneinfo import ZoneInfo
from core.models import StudentProfile
from .models import BattleRoom, BattleMatchPlayer
from .match_loop import _rules_for
from .tiers import TIERS

LAGOS = ZoneInfo("Africa/Lagos")


def _card(room):
    a, b, spectators = [], [], 0
    for p in room.participants.all():
        if p.role == "spectator":
            spectators += 1
        elif p.team == "A":
            a.append(p.student.user.username)
        elif p.team == "B":
            b.append(p.student.user.username)
    return {"code": room.code, "mode": room.mode or "", "a": a[:3], "b": b[:3],
            "spectators": spectators, "host": room.leader.user.username,
            "players": len(a) + len(b)}


def _rooms(**filters):
    # needs a connected participant, so abandoned rooms never show up
    qs = (BattleRoom.objects.filter(participants__connection_status="connected", **filters)
          .distinct().select_related("leader__user")
          .prefetch_related("participants__student__user").order_by("-id")[:8])
    return [_card(r) for r in qs]


def _history(me):
    out = []
    rows = (BattleMatchPlayer.objects.filter(student=me)
            .select_related("match").order_by("-match__ended_at")[:10])
    for r in rows:
        m = r.match
        mine, theirs = ((m.team_a_score, m.team_b_score) if r.team == "A"
                        else (m.team_b_score, m.team_a_score))
        opp = "B" if r.team == "A" else "A"
        opps = [p.student.user.username
                for p in m.players.select_related("student__user") if p.team == opp][:3]
        out.append({
            "result": "draw" if m.winner is None else ("win" if r.won else "loss"),
            "score": f"{mine} – {theirs}", "mode": m.mode, "opponents": opps,
            "when": m.ended_at.astimezone(LAGOS).strftime("%d %b, %H:%M") if m.ended_at else "",
        })
    return out


def landing_context(request):
    me = StudentProfile.objects.filter(user=request.user).first()
    return {
        "active": _rooms(status="in_progress"),
        "live": _rooms(status="waiting", is_public=True),
        "history": _history(me) if me else [],
        "rules_alt": _rules_for("alternating", 10),
        "rules_buzz": _rules_for("buzzer", 10),
        "tier_list": list(reversed(TIERS)),
    }