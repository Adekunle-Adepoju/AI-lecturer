import json
import time

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.http import HttpResponse
from django.views.decorators.http import require_POST
from django.shortcuts import redirect, render
from django.http import Http404
from datetime import timedelta

from core.models import TopicSession
from .models import DailyActivity, ModuleDaily
from .record import LAGOS, bump, lagos_today
from . import reports

MAX_ACTIVE, MAX_TTS = 180, 180        # per request; legit flushes are ~30s
MAX_COUNT = 50


def _int(v, hi):
    try:
        return max(0, min(int(v), hi))
    except (TypeError, ValueError):
        return 0


@require_POST
def ingest(request):
    user = request.user
    if not user.is_authenticated:
        return HttpResponse(status=204)                    # never redirect a beacon
    profile = getattr(user, "profile", None)
    if profile is None:
        return HttpResponse(status=204)
    if not getattr(settings, "ANALYTICS_TRACK_STAFF", False) and (
        user.is_superuser or profile.is_staff_member
    ):
        return HttpResponse(status=204)

    try:
        p = json.loads(request.POST.get("payload", ""))
        assert isinstance(p, dict) and p.get("v") == 1
    except (ValueError, AssertionError):
        return HttpResponse(status=400)

    # cheap per-user rate limit (legit use is a few requests/minute)
    rl = f"pulse:rl:{profile.pk}:{int(time.time() // 60)}"
    cache.add(rl, 0, 120)
    if cache.incr(rl) > 30:
        return HttpResponse(status=429)

    # drop duplicate deliveries (client retries)
    sid, n = str(p.get("sid", ""))[:64], _int(p.get("n"), 10**9)
    if sid and not cache.add(f"pulse:seen:{sid}:{n}", 1, 3600):
        return HttpResponse(status=204)

    active = _int(p.get("active"), MAX_ACTIVE)
    tts = _int(p.get("tts"), MAX_TTS)
    plays, done, parts = (_int(p.get(k), MAX_COUNT) for k in ("plays", "done", "parts"))
    day = lagos_today()

    with transaction.atomic():
        bump(DailyActivity, dict(student=profile, date=day),
             active_seconds=active, tts_seconds=tts)

        ts_id = _int(p.get("ts"), 2**31 - 1)
        if ts_id and (active or tts or plays or done or parts):
            ts = (TopicSession.objects.select_related("session")
                  .filter(pk=ts_id, session__student=profile).first())   # authoritative names
            if ts:
                bump(ModuleDaily,
                     dict(student=profile, date=day, course_code=ts.session.course_code,
                          week_number=ts.session.week_number, topic_name=ts.topic_name),
                     read_seconds=active, tts_seconds=tts, tts_plays=plays,
                     tts_completions=done, parts_viewed=parts)

    return HttpResponse(status=204)

def _is_staff(user):
    p = getattr(user, "profile", None)
    return user.is_authenticated and (user.is_superuser or bool(p and p.is_staff_member))


def dashboard(request):
    if not request.user.is_authenticated:
        return redirect("login")
    if not _is_staff(request.user):
        raise Http404                      # students never learn this page exists

    try:
        days = int(request.GET.get("days", 30))
    except ValueError:
        days = 30
    days = days if days in (7, 30, 90) else 30

    # Daily active users, with zero-filled gaps so the chart has no holes
    start = lagos_today() - timedelta(days=days - 1)
    by_date = {r["date"]: r["users"] for r in reports.dau_series(days)}
    series = [{"date": start + timedelta(days=i),
               "users": by_date.get(start + timedelta(days=i), 0)} for i in range(days)]
    peak = max([s["users"] for s in series] + [1])
    for s in series:
        s["pct"] = round(s["users"] / peak * 100)

    unique = reports.active_users(days)
    avg_dau = round(sum(s["users"] for s in series) / days, 1)

    tts = reports.tts_summary(days)
    tts["finish_pct"] = round(tts["finish_rate"] * 100) if tts["finish_rate"] is not None else None
    tts["adoption_pct"] = (round(tts["adoption_of_active_users"] * 100)
                           if tts["adoption_of_active_users"] is not None else None)

    last = DailyActivity.objects.order_by("-last_seen_at").values_list("last_seen_at", flat=True).first()

    return render(request, "engagement/dashboard.html", {
        "days": days,
        "series": series,
        "today_users": series[-1]["users"],
        "avg_dau": avg_dau,
        "unique": unique,
        "wau": reports.active_users(7),
        "stickiness": round(avg_dau / unique * 100) if unique else None,
        "tts": tts,
        "modules": reports.module_report(days),
        "last_pulse": last.astimezone(LAGOS).strftime("%d %b %Y, %H:%M") if last else None,
        "tracking_staff": getattr(settings, "ANALYTICS_TRACK_STAFF", False),
    })