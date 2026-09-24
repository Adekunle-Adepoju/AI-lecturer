from datetime import timedelta
from django.db.models import Count, Q, Sum
from .models import DailyActivity, ModuleDaily
from .record import lagos_today

# An "engaged" day = ≥30s active OR any TTS listening. Because raw seconds are
# stored, changing this re-defines DAU for ALL history — no migration needed.
MIN_ACTIVE_SECONDS = 30
ENGAGED = Q(active_seconds__gte=MIN_ACTIVE_SECONDS) | Q(tts_seconds__gt=0)


def _since(days):
    return lagos_today() - timedelta(days=days - 1)


def dau_series(days=30):
    return list(DailyActivity.objects.filter(date__gte=_since(days)).filter(ENGAGED)
                .values("date").annotate(users=Count("student")).order_by("date"))


def active_users(days):
    return (DailyActivity.objects.filter(date__gte=_since(days)).filter(ENGAGED)
            .values("student").distinct().count())


def headline():
    series = dau_series(30)
    avg_dau = sum(r["users"] for r in series) / 30
    mau = active_users(30)
    return {"avg_dau_30d": round(avg_dau, 1), "wau": active_users(7), "mau": mau,
            "dau_over_mau": round(avg_dau / mau, 2) if mau else None}


def module_report(days=None):
    qs = ModuleDaily.objects.all()
    if days:
        qs = qs.filter(date__gte=_since(days))
    rows = (qs.values("course_code", "week_number", "topic_name")
              .annotate(readers=Count("student", distinct=True), read=Sum("read_seconds"),
                        listen=Sum("tts_seconds"), plays=Sum("tts_plays"),
                        finished=Sum("tts_completions"), parts=Sum("parts_viewed"))
              .order_by("course_code", "week_number", "topic_name"))
    out = []
    for r in rows:
        n = r["readers"] or 1
        out.append({**r, "avg_read_min": round((r["read"] or 0) / n / 60, 1),
                    "avg_listen_min": round((r["listen"] or 0) / n / 60, 1)})
    return out


def tts_summary(days=30):
    qs = ModuleDaily.objects.filter(date__gte=_since(days))
    t = qs.aggregate(sec=Sum("tts_seconds"), plays=Sum("tts_plays"), done=Sum("tts_completions"))
    listeners = qs.filter(tts_seconds__gt=0).values("student").distinct().count()
    active, plays = active_users(days), t["plays"] or 0
    return {"listen_hours": round((t["sec"] or 0) / 3600, 1), "listeners": listeners, "plays": plays,
            "finish_rate": round((t["done"] or 0) / plays, 2) if plays else None,
            "adoption_of_active_users": round(listeners / active, 2) if active else None}