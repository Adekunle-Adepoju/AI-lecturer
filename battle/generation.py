import json
import re
import time

from django.conf import settings
from google import genai
from google.genai import types

from core.models import PreGeneratedLesson
from .models import BattleQuestion, BattleAnswerOption, BattleQuestionGenerationChunk
from .prompts import BATTLE_QUESTION_GENERATION_PROMPT
from django.db import close_old_connections

# Catches source-referencing phrasing the prompt explicitly forbids
# ("according to the lecture", "as stated in the slides", "the material
# says", "given in the notes", etc.) — the model occasionally slips past
# this rule despite the explicit instruction, so this is a hard backstop:
# any question matching this is silently skipped rather than saved for
# review, since there's no legitimate case where this phrasing is correct.
SOURCE_REFERENCE_RE = re.compile(
    r"\b(according to|as (?:stated|given|shown|mentioned) in|based on the|"
    r"per the|the (?:lecture|slide|slides|material|notes|course) (?:says|states|shows|mentions)|"
    r"(?:in|from) the (?:lecture|slides?|material|notes|course))\b",
    re.IGNORECASE,
)


def _repair_and_parse_json_array(text):
    clean = text.replace("```json", "").replace("```", "").strip()
    start = clean.find("[")
    end = clean.rfind("]")
    if start != -1 and end != -1:
        clean = clean[start:end + 1]

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    repaired = []
    in_string = False
    escape_next = False
    for ch in clean:
        if escape_next:
            repaired.append(ch)
            escape_next = False
            continue
        if ch == "\\":
            repaired.append(ch)
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            repaired.append(ch)
            continue
        if in_string and ch == "\n":
            repaired.append("\\n")
            continue
        if in_string and ch == "\t":
            repaired.append("\\t")
            continue
        if in_string and ch == "\r":
            continue
        repaired.append(ch)

    return json.loads("".join(repaired))


MODELS = ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"]
ATTEMPTS_PER_MODEL = 3      # 503 only
RETRY_WAIT_SECONDS = 20


class AllModelsUnavailable(RuntimeError):
    """Every model in MODELS was overloaded or out of quota."""

QUESTIONS_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "question": {"type": "STRING"},
            "correct_answer": {"type": "STRING"},
            "distractors": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "minItems": 3,
                "maxItems": 3,
            },
        },
        "required": ["question", "correct_answer", "distractors"],
    },
}

def _call_with_retry(client, prompt, log):
    reasons = []
    for model in MODELS:
        for attempt in range(1, ATTEMPTS_PER_MODEL + 1):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        max_output_tokens=8000,
                        response_mime_type="application/json",
                        response_schema=QUESTIONS_SCHEMA,
                    ),
                )
                if not response.text or not response.text.strip():
                    raise ValueError("Empty response from model")
                return response.text
            except Exception as e:
                error_str = str(e)

                if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                    log(f"    {model}: 429 quota hit, moving to next model")
                    reasons.append(f"{model}: 429")
                    break

                if "503" in error_str or "UNAVAILABLE" in error_str:
                    if attempt < ATTEMPTS_PER_MODEL:
                        log(f"    {model}: 503 overloaded (try {attempt}/{ATTEMPTS_PER_MODEL}), "
                            f"waiting {RETRY_WAIT_SECONDS}s...")
                        time.sleep(RETRY_WAIT_SECONDS)
                        continue
                    log(f"    {model}: still 503 after {ATTEMPTS_PER_MODEL} tries, moving to next model")
                    reasons.append(f"{model}: 503 x{ATTEMPTS_PER_MODEL}")
                    break

                # Any other error is not an outage, so fail this chunk as before.
                raise RuntimeError(f"Generation failed on {model}: {e}") from e

    raise AllModelsUnavailable("All models failed (" + "; ".join(reasons) + ")")

_VISUAL_FENCE_RE = re.compile(r"```(?:svg|mermaid|json_chart)[^\n]*\n.*?```", re.DOTALL)
MAX_LESSON_CHARS = 30000


def eligible_lessons(level, department, course_code=None):
    """Lessons that exist, have text, and are not cut off. No lesson, no questions."""
    qs = PreGeneratedLesson.objects.filter(
        course__level=level, course__department=department,
        is_truncated=False, content_chunk__regex=r"\S",
    ).select_related("course")
    if course_code:
        qs = qs.filter(course__course_code=course_code)
    return qs

def run_battle_question_generation(
    level, department, per_chunk=8, sleep_seconds=3.5, course_filter=None,
    retry_failed=False, limit=10, include_completed=False, log=print,
):
    client = genai.Client(api_key=settings.GEMINI_API_KEY_BATTLE)

    lessons = list(
        eligible_lessons(level, department, course_filter)
        .order_by("course__course_code", "week_number", "topic_title")
    )

    done_statuses = ["COMPLETED"] if retry_failed else ["COMPLETED", "FAILED"]
    if include_completed:
        done_statuses = [] if retry_failed else ["FAILED"]
    done_ids = set(
        BattleQuestionGenerationChunk.objects.filter(
            source_lesson_id__in=[l.id for l in lessons], status__in=done_statuses,
        ).values_list("source_lesson_id", flat=True)
    )
    pending = [l for l in lessons if l.id not in done_ids]
    if include_completed:
        from django.db.models import Count
        counts = dict(
            BattleQuestion.objects.filter(source_lesson_id__in=[l.id for l in pending])
            .exclude(status="retired")
            .values_list("source_lesson_id").annotate(n=Count("id"))
        )
        pending.sort(key=lambda l: counts.get(l.id, 0))   # fewest questions first
    if limit:
        pending = pending[:limit]

    log(f"{len(pending)} lesson(s) to process.")

    total_generated = 0
    total_failed = 0
    attempted = 0
    failures = []

    for lesson in pending:
        attempted += 1
        course = lesson.course
        gen, _ = BattleQuestionGenerationChunk.objects.get_or_create(source_lesson=lesson)

        prompt = BATTLE_QUESTION_GENERATION_PROMPT.format(
            course_code=course.course_code,
            topic_name=lesson.topic_title,
            target_count=per_chunk,
            slide_context=_VISUAL_FENCE_RE.sub("[diagram]", lesson.content_chunk)[:MAX_LESSON_CHARS],
        )
        existing = list(
            BattleQuestion.objects.filter(source_lesson=lesson)
            .values_list("stem", flat=True)[:80]
        )
        if existing:
            prompt += (
                "\n\nQUESTIONS ALREADY WRITTEN FOR THIS LESSON. Do not repeat or reword any of "
                "them. Test different facts, concepts or calculations from the lesson:\n"
                + "\n".join(f"- {s}" for s in existing)
            )
        seen = {re.sub(r"\W+", " ", s).strip().lower() for s in existing}
        log(f"  {course.course_code} — {lesson.topic_title} ...")

        try:
            questions_data = None
            response_text = ""
            parse_error = None
            for parse_try in (1, 2):
                response_text = _call_with_retry(client, prompt, log)
                close_old_connections()
                try:
                    questions_data = _repair_and_parse_json_array(response_text)
                    break
                except (json.JSONDecodeError, ValueError) as e:
                    parse_error = e
                    log(f"    unparseable reply (try {parse_try}/2): {e}")
            if questions_data is None:
                raise ValueError(
                    f"{parse_error}\n\nRAW RESPONSE (truncated):\n{response_text[:1500]}"
                ) from parse_error

            created = 0
            skipped_references = 0
            for q in questions_data:
                stem = (q.get("question") or "").strip()
                correct = (q.get("correct_answer") or "").strip()
                distractors = [d.strip() for d in (q.get("distractors") or []) if d.strip()]

                if not stem or not correct or len(distractors) < 3:
                    continue
                if SOURCE_REFERENCE_RE.search(stem):
                    skipped_references += 1
                    continue
                norm = re.sub(r"\W+", " ", stem).strip().lower()
                if norm in seen:
                    continue
                seen.add(norm)

                question = BattleQuestion.objects.create(
                    course_code=course.course_code,
                    level=course.level,
                    department=course.department,
                    source_lesson=lesson,
                    question_type="static",
                    stem=stem,
                    status="pending_review",
                )
                BattleAnswerOption.objects.create(question=question, text=correct, is_correct=True)
                for d in distractors:
                    BattleAnswerOption.objects.create(question=question, text=d, is_correct=False)
                created += 1

            gen.status = "COMPLETED"
            gen.questions_generated = created
            gen.error_message = ""
            gen.save(update_fields=["status", "questions_generated", "error_message", "updated_at"])

            total_generated += created
            log(f"    -> {created} question(s) saved as pending_review"
                + (f" ({skipped_references} skipped for source-referencing phrasing)" if skipped_references else ""))

        except AllModelsUnavailable as e:
            # Outage: stop the whole job. The chunk is NOT marked FAILED,
            # so the next run picks it up normally.
            total_failed += 1
            failures.append(f"{course.course_code} — {lesson.topic_title}: {e}")
            log(f"    -> ALL MODELS UNAVAILABLE, ending job: {e}")
            break
        except Exception as e:
            close_old_connections()
            gen.status = "FAILED"
            gen.error_message = str(e)
            gen.save(update_fields=["status", "error_message", "updated_at"])
            total_failed += 1
            failures.append(f"{course.course_code} — {lesson.topic_title}: {e}")
            log(f"    -> FAILED: {e}")

        time.sleep(sleep_seconds)

    return {
        "chunks_processed": attempted,
        "questions_generated": total_generated,
        "chunks_failed": total_failed,
        "failures": failures,
    }