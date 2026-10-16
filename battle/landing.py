from zoneinfo import ZoneInfo
from core.models import StudentProfile
from .models import BattleRoom, BattleMatchPlayer, BattleParticipant, BattleStats
from .match_loop import _rules_for
from .tiers import TIERS, tier_for
from datetime import timedelta
from django.db.models import Count
from django.utils import timezone

LAGOS = ZoneInfo("Africa/Lagos")

def _dept_ladder(me, limit=5):
    if me is None or not me.department:
        return []
    now = timezone.now().astimezone(LAGOS)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (BattleMatchPlayer.objects
            .filter(won=True, match__ended_at__gte=week_start, student__department=me.department)
            .values("student__user__username").annotate(w=Count("id"))
            .order_by("-w", "student__user__username")[:limit])
    return [{"rank": i, "name": r["student__user__username"], "wins": r["w"]}
            for i, r in enumerate(rows, start=1)]

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
    # public rooms only, and needs a connected participant so abandoned rooms never show up
    qs = (BattleRoom.objects.filter(is_public=True, participants__connection_status="connected", **filters)
          .distinct().select_related("leader__user")
          .prefetch_related("participants__student__user").order_by("-id")[:8])
    return [_card(r) for r in qs]


def _active_players(limit=12, exclude_id=None):
    """Everyone with Rovea open right now, wherever they are in the app."""
    import time
    from django.core.cache import cache
    from .consumers import PRESENCE_KEY, PRESENCE_TTL
    now = time.time()
    data = cache.get(PRESENCE_KEY) or {}
    live = sorted(
        ((ts, uid, name) for uid, (ts, name) in data.items()
         if now - ts < PRESENCE_TTL and uid != exclude_id),
        reverse=True,
    )
    people = [{"id": uid, "name": name} for _, uid, name in live]
    names = [p["name"] for p in people]
    return {"shown": names[:limit], "players": people[:limit],
            "more": max(0, len(names) - limit), "total": len(names)}

def _top_players(limit=5):
    rows = (BattleStats.objects.filter(wins__gt=0)
            .select_related("student__user").order_by("-wins", "-best_streak")[:limit])
    return [{"rank": i, "name": s.student.user.username, "wins": s.wins,
             "win_rate": s.win_rate, "tier": tier_for(s.wins)}
            for i, s in enumerate(rows, start=1)]


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
        "live": _rooms(status="waiting"),
        "active_players": _active_players(),
        "top_players": _top_players(),
        "history": _history(me) if me else [],
        "rules_alt": _rules_for("alternating", 10),
        "rules_buzz": _rules_for("buzzer", 10),
        "tier_list": list(reversed(TIERS)),
        "dept_ladder": _dept_ladder(me),
        "dept_name": me.department if me else "",
    }