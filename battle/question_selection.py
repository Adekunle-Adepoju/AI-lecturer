import random
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import BattleQuestion, StudentQuestionExposure

EXPOSURE_WINDOW_DAYS = 14


def _topic_of(q):
    if q.source_lesson_id:
        return q.source_lesson.topic_title
    if q.source_chunk_id:
        return q.source_chunk.topic_name
    return ""


def select_match_questions(participant_student_ids, count, levels, course_codes=None,
                           exclude_ids=(), departments=None):
    cutoff = timezone.now() - timedelta(days=EXPOSURE_WINDOW_DAYS)

    seen_ids = set(
        StudentQuestionExposure.objects.filter(
            student_id__in=participant_student_ids,
            last_seen_at__gte=cutoff,
        ).values_list("question_id", flat=True)
    )

    base_qs = (
        BattleQuestion.objects.filter(status="approved", level__in=levels, question_type="static")
        .exclude(id__in=list(exclude_ids))
        .select_related("source_lesson", "source_chunk")
        .prefetch_related("options")
    )
    if course_codes:
        base_qs = base_qs.filter(course_code__in=course_codes)
    if departments:
        # blank department = general question; otherwise it must match a player's department
        base_qs = base_qs.filter(Q(department="") | Q(department__in=departments))

    unseen = list(base_qs.exclude(id__in=seen_ids))
    seen = list(base_qs.filter(id__in=seen_ids))
    random.shuffle(unseen)
    random.shuffle(seen)

    resolved = []
    for q in unseen + seen:
        if len(resolved) >= count:
            break
        options = list(q.options.all())
        correct = [o for o in options if o.is_correct]
        wrong = [o for o in options if not o.is_correct]
        if not correct or len(wrong) < 3:
            continue

        serve_options = [correct[0]] + random.sample(wrong, 3)
        random.shuffle(serve_options)
        correct_index = next(i for i, o in enumerate(serve_options) if o.is_correct)

        resolved.append({
            "question_id": q.id,
            "stem": q.stem,
            "options": [o.text for o in serve_options],
            "correct_index": correct_index,
            "topic": _topic_of(q),
            "explanation": q.explanation or "",
        })
    return resolved