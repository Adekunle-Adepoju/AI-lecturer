import json
import re
import time

from django.conf import settings
from google import genai
from google.genai import types

from core.models import SlideTopicChunk
from .models import BattleQuestion, BattleAnswerOption, BattleQuestionGenerationChunk
from .prompts import BATTLE_QUESTION_GENERATION_PROMPT

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


def _call_with_retry(client, prompt, log, max_retries=3):
    last_error = None
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
                config=types.GenerateContentConfig(max_output_tokens=4000),
            )
            if not response.text or not response.text.strip():
                raise ValueError("Empty response from model")
            return response.text
        except Exception as e:
            last_error = e
            error_str = str(e)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                log("    429 hit — sleeping 20s and retrying...")
                time.sleep(20)
                continue
            if ("503" in error_str or "UNAVAILABLE" in error_str) and attempt < max_retries - 1:
                log("    503 overloaded — sleeping 10s and retrying...")
                time.sleep(10)
                continue
            break
    raise RuntimeError(f"Generation failed after {max_retries} attempts: {last_error}")


def run_battle_question_generation(
    per_chunk=8, sleep_seconds=3.5, course_filter=None,
    retry_failed=False, limit=10, log=print,
):
    """Shared logic behind both the manage.py command and the staff-portal
    button. Resumable: only touches SlideTopicChunks that don't already
    have a COMPLETED (or, unless retry_failed, FAILED) generation record.
    Returns a dict summary for the caller to display."""
    client = genai.Client(api_key=settings.GEMINI_API_KEY_BATTLE)

    eligible_chunks = SlideTopicChunk.objects.filter(
        is_empty=False, slide__level__in=["300", "400"],
    ).select_related("slide")

    if course_filter:
        eligible_chunks = eligible_chunks.filter(slide__course_code=course_filter)

    done_statuses = ["COMPLETED"] if retry_failed else ["COMPLETED", "FAILED"]
    already_done_ids = set(
        BattleQuestionGenerationChunk.objects.filter(
            status__in=done_statuses
        ).values_list("source_chunk_id", flat=True)
    )
    pending_chunks = [c for c in eligible_chunks if c.id not in already_done_ids]

    if limit:
        pending_chunks = pending_chunks[:limit]

    log(f"{len(pending_chunks)} chunk(s) to process.")

    total_generated = 0
    total_failed = 0
    failures = []

    for chunk in pending_chunks:
        gen_chunk, _ = BattleQuestionGenerationChunk.objects.get_or_create(source_chunk=chunk)

        prompt = BATTLE_QUESTION_GENERATION_PROMPT.format(
            course_code=chunk.slide.course_code,
            topic_name=chunk.topic_name,
            target_count=per_chunk,
            slide_context=chunk.chunk_text[:12000],
        )

        log(f"  {chunk.slide.course_code} — {chunk.topic_name} ...")

        try:
            response_text = _call_with_retry(client, prompt, log)
            try:
                questions_data = _repair_and_parse_json_array(response_text)
            except (json.JSONDecodeError, ValueError) as parse_error:
                raise ValueError(
                    f"{parse_error}\n\nRAW RESPONSE (truncated):\n{response_text[:1500]}"
                ) from parse_error

            if not isinstance(questions_data, list) or not questions_data:
                raise ValueError(f"Model returned no usable questions\n\nRAW RESPONSE (truncated):\n{response_text[:1500]}")

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

                question = BattleQuestion.objects.create(
                    course_code=chunk.slide.course_code,
                    level=chunk.slide.level,
                    source_chunk=chunk,
                    question_type="static",
                    stem=stem,
                    status="pending_review",
                )
                BattleAnswerOption.objects.create(question=question, text=correct, is_correct=True)
                for d in distractors:
                    BattleAnswerOption.objects.create(question=question, text=d, is_correct=False)
                created += 1

            gen_chunk.status = "COMPLETED"
            gen_chunk.questions_generated = created
            gen_chunk.error_message = ""
            gen_chunk.save(update_fields=["status", "questions_generated", "error_message", "updated_at"])

            total_generated += created
            log(f"    -> {created} question(s) saved as pending_review"
                + (f" ({skipped_references} skipped for source-referencing phrasing)" if skipped_references else ""))

        except Exception as e:
            gen_chunk.status = "FAILED"
            gen_chunk.error_message = str(e)
            gen_chunk.save(update_fields=["status", "error_message", "updated_at"])
            total_failed += 1
            failures.append(f"{chunk.slide.course_code} — {chunk.topic_name}: {e}")
            log(f"    -> FAILED: {e}")

        time.sleep(sleep_seconds)

    return {
        "chunks_processed": len(pending_chunks),
        "questions_generated": total_generated,
        "chunks_failed": total_failed,
        "failures": failures,
    }