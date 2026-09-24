from zoneinfo import ZoneInfo
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

LAGOS = ZoneInfo("Africa/Lagos")


def lagos_today():
    return timezone.now().astimezone(LAGOS).date()


def bump(model, lookup, **counters):
    """Atomic upsert: add `counters` to the row matching `lookup`, creating it if needed."""
    now = timezone.now()
    updates = {k: F(k) + v for k, v in counters.items() if v}
    updates["last_seen_at"] = now
    if model.objects.filter(**lookup).update(**updates):
        return
    try:
        with transaction.atomic():
            model.objects.create(**lookup, **counters, first_seen_at=now, last_seen_at=now)
    except IntegrityError:            # lost a race with a parallel request
        model.objects.filter(**lookup).update(**updates)