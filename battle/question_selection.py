import random
from datetime import timedelta

from django.utils import timezone

from .models import BattleQuestion, StudentQuestionExposure

EXPOSURE_WINDOW_DAYS = 14


def select_match_questions(participant_student_ids, count, levels,
                           course_codes=None, exclude_ids=()):
    cutoff = timezone.now() - timedelta(days=EXPOSURE_WINDOW_DAYS)

    seen_ids = set(
        StudentQuestionExposure.objects.filter(
            student_id__in=participant_student_ids,
            last_seen_at__gte=cutoff,
        ).values_list("question_id", flat=True)
    )

    base_qs = BattleQuestion.objects.filter(
        status="approved", level__in=levels, question_type="static",
    ).exclude(id__in=list(exclude_ids)).prefetch_related("options")

    if course_codes:
        base_qs = base_qs.filter(course_code__in=course_codes)

    # Unseen questions first, then top up with recently seen ones.
    unseen = list(base_qs.exclude(id__in=seen_ids))
    seen = list(base_qs.filter(id__in=seen_ids))
    random.shuffle(unseen)
    random.shuffle(seen)
    pool_list = unseen + seen

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