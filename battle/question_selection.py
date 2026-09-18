import random
from datetime import timedelta

from django.utils import timezone

from .models import BattleQuestion, StudentQuestionExposure

EXPOSURE_WINDOW_DAYS = 14


def select_match_questions(participant_student_ids, count, levels=("300", "400")):
    """Random across 300L/400L per the agreed rule — no course selection.
    Prefers questions no participant has seen recently; falls back to the
    full approved pool if exposure filtering leaves too few to fill a
    match. This fallback is the honest state of things right now — with
    only a handful of approved questions total, exposure filtering will
    almost always trigger it. It stops mattering once the bank is bigger."""
    cutoff = timezone.now() - timedelta(days=EXPOSURE_WINDOW_DAYS)

    seen_ids = set(
        StudentQuestionExposure.objects.filter(
            student_id__in=participant_student_ids,
            last_seen_at__gte=cutoff,
        ).values_list("question_id", flat=True)
    )

    base_qs = BattleQuestion.objects.filter(
        status="approved", level__in=levels, question_type="static",
    ).prefetch_related("options")

    pool_list = list(base_qs.exclude(id__in=seen_ids))
    if len(pool_list) < count:
        pool_list = list(base_qs)  # exposure filter dropped entirely, not ranked

    random.shuffle(pool_list)

    resolved = []
    for q in pool_list:
        if len(resolved) >= count:
            break
        options = list(q.options.all())
        correct = [o for o in options if o.is_correct]
        wrong = [o for o in options if not o.is_correct]
        if not correct or len(wrong) < 3:
            continue  # malformed question — skip rather than serve broken data

        chosen_wrong = random.sample(wrong, min(3, len(wrong)))
        serve_options = [correct[0]] + chosen_wrong
        random.shuffle(serve_options)
        correct_index = next(i for i, o in enumerate(serve_options) if o.is_correct)

        resolved.append({
            "question_id": q.id,
            "stem": q.stem,
            "options": [o.text for o in serve_options],
            "correct_index": correct_index,
        })
    return resolved