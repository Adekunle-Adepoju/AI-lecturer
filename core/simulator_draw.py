"""Assemble a test by drawing approved, verified questions from the bank."""
import random

from .models import SimulatorQuestion, SimulatorTest

OBJECTIVE_COUNT = 20
THEORY_COUNT = 3
MIXED_MCQ = 10
MIXED_LONG = 2

TEST_MODES = {"objective": "mcq", "theory": "theory", "mixed": "mixed"}
# minimum (mcq, written) a draw must reach, or the test is refused
MIN_REQUIRED = {"objective": (8, 0), "theory": (0, 2), "mixed": (5, 1)}

LETTERS = "ABCD"


class NotEnoughQuestions(Exception):
    pass


def seen_bank_ids(profile):
    seen = set()
    for questions in SimulatorTest.objects.filter(student=profile).values_list("questions", flat=True):
        for q in questions or []:
            if isinstance(q, dict) and q.get("bank_id"):
                seen.add(q["bank_id"])
    return seen


def _pools(course_code, level, topics, want_mcq, seen):
    qs = SimulatorQuestion.objects.filter(
        course_code=course_code, level=level, topic_name__in=topics,
        status="approved", verified=True,
    )
    qs = qs.filter(question_type="mcq") if want_mcq else qs.exclude(question_type="mcq")
    pools = {t: [] for t in topics}
    for q in qs:
        pools[q.topic_name].append(q)
    for items in pools.values():
        random.shuffle(items)
        items.sort(key=lambda q: q.id in seen)  # stable: unseen first, shuffled within each group
    return pools


def _pick(pools, total):
    """Round-robin across topics: an even spread, and a topic that runs out
    of questions automatically hands its share to the others."""
    order = list(pools)
    random.shuffle(order)
    taken = {t: 0 for t in order}
    remaining = total
    while remaining > 0:
        progressed = False
        for t in order:
            if remaining == 0:
                break
            if taken[t] < len(pools[t]):
                taken[t] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    picked = []
    for t in order:
        picked.extend(pools[t][:taken[t]])
    return picked


def _serialize(q):
    c = q.content
    out = {
        "bank_id": q.id,
        "question_type": q.question_type,
        "topic": q.topic_name,
        "course_code": q.course_code,
        "marks": q.marks,
        "question": c["question"],
    }
    if q.question_type == "mcq":
        order = list(range(4))
        random.shuffle(order)
        out["options"] = [f"{LETTERS[n]}. {c['options'][i]}" for n, i in enumerate(order)]
        out["correct_index"] = order.index(c["correct_index"])
        out["explanation"] = c.get("explanation", "")
    else:
        out["model_answer"] = c["model_answer"]
        out["marking_scheme"] = c["marking_scheme"]
    return out


def draw_test(profile, course_code, level, topics, test_mode):
    """Returns (questions, question_format). Raises NotEnoughQuestions."""
    if test_mode not in TEST_MODES:
        raise ValueError(f"Unknown test mode: {test_mode}")
    seen = seen_bank_ids(profile)

    mcq, longs = [], []
    if test_mode in ("objective", "mixed"):
        want = OBJECTIVE_COUNT if test_mode == "objective" else MIXED_MCQ
        mcq = _pick(_pools(course_code, level, topics, True, seen), want)
    if test_mode in ("theory", "mixed"):
        want = THEORY_COUNT if test_mode == "theory" else MIXED_LONG
        longs = _pick(_pools(course_code, level, topics, False, seen), want)

    need_mcq, need_long = MIN_REQUIRED[test_mode]
    if len(mcq) < need_mcq or len(longs) < need_long:
        raise NotEnoughQuestions(
            f"There aren't enough approved questions for this combination yet "
            f"(found {len(mcq)} multiple-choice and {len(longs)} written). "
            f"Add more topics or try a different test type."
        )

    random.shuffle(mcq)
    random.shuffle(longs)
    return [_serialize(q) for q in mcq + longs], TEST_MODES[test_mode]