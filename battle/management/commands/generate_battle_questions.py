import json
import re
import time

from django.core.management.base import BaseCommand
from django.conf import settings
from google import genai
from google.genai import types

from core.models import SlideTopicChunk
from battle.models import (
    BattleQuestion, BattleAnswerOption, BattleQuestionGenerationChunk,
)
from battle.prompts import BATTLE_QUESTION_GENERATION_PROMPT


def _repair_and_parse_json_array(text):
    """Parse a JSON array from model output, tolerating raw control
    characters inside string values. Same approach as core.views'
    _repair_and_parse_json_array — duplicated locally rather than
    imported, since that one is a private helper in a views module not
    meant to be depended on from elsewhere."""
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


class Command(BaseCommand):
    help = (
        "Generate battle-mode MCQ questions from non-empty SlideTopicChunks "
        "for 300L/400L courses. Resumable — already-COMPLETED chunks are "
        "skipped by default. All generated questions land as pending_review "
        "and require staff approval before entering the servable pool."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--per-chunk", type=int, default=8,
            help="Target number of questions to request per topic chunk (default 8).",
        )
        parser.add_argument(
            "--sleep", type=float, default=3.5,
            help="Seconds to sleep between Gemini calls (default 3.5).",
        )
        parser.add_argument(
            "--course", type=str, default=None,
            help="Restrict to a single course code, e.g. PGG313.",
        )
        parser.add_argument(
            "--retry-failed", action="store_true",
            help="Also reprocess chunks previously marked FAILED (default: skip them too).",
        )
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Process at most this many chunks this run — useful for a test batch.",
        )

    def handle(self, *args, **options):
        per_chunk = options["per_chunk"]
        sleep_seconds = options["sleep"]
        course_filter = options["course"]
        retry_failed = options["retry_failed"]
        limit = options["limit"]

        client = genai.Client(api_key=settings.GEMINI_API_KEY_BATTLE)

        eligible_chunks = SlideTopicChunk.objects.filter(
            is_empty=False,
            slide__level__in=["300", "400"],
        ).select_related("slide")

        if course_filter:
            eligible_chunks = eligible_chunks.filter(slide__course_code=course_filter)

        # Exclude chunks already COMPLETED. Optionally also exclude FAILED
        # ones (default), or retry them if --retry-failed is passed.
        done_statuses = ["COMPLETED"] if retry_failed else ["COMPLETED", "FAILED"]
        already_done_ids = set(
            BattleQuestionGenerationChunk.objects.filter(
                status__in=done_statuses
            ).values_list("source_chunk_id", flat=True)
        )
        pending_chunks = [c for c in eligible_chunks if c.id not in already_done_ids]

        if limit:
            pending_chunks = pending_chunks[:limit]

        self.stdout.write(f"{len(pending_chunks)} chunk(s) to process.")

        total_generated = 0
        total_failed = 0

        for chunk in pending_chunks:
            gen_chunk, _ = BattleQuestionGenerationChunk.objects.get_or_create(
                source_chunk=chunk
            )

            prompt = BATTLE_QUESTION_GENERATION_PROMPT.format(
                course_code=chunk.slide.course_code,
                topic_name=chunk.topic_name,
                target_count=per_chunk,
                slide_context=chunk.chunk_text[:12000],
            )

            self.stdout.write(f"  {chunk.slide.course_code} — {chunk.topic_name} ...")

            try:
                response_text = self._call_with_retry(client, prompt)
                try:
                    questions_data = _repair_and_parse_json_array(response_text)
                except (json.JSONDecodeError, ValueError) as parse_error:
                    # Preserve what the model actually returned — the parser's
                    # own error only tells you where it gave up, not what
                    # broke, and without the raw text this is undiagnosable
                    # if it recurs.
                    raise ValueError(
                        f"{parse_error}\n\nRAW RESPONSE (truncated):\n{response_text[:1500]}"
                    ) from parse_error

                if not isinstance(questions_data, list) or not questions_data:
                    raise ValueError(f"Model returned no usable questions\n\nRAW RESPONSE (truncated):\n{response_text[:1500]}")

                created = 0
                for q in questions_data:
                    stem = (q.get("question") or "").strip()
                    correct = (q.get("correct_answer") or "").strip()
                    distractors = [d.strip() for d in (q.get("distractors") or []) if d.strip()]

                    if not stem or not correct or len(distractors) < 3:
                        # Skip malformed individual items rather than
                        # failing the whole chunk over one bad entry.
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
                self.stdout.write(self.style.SUCCESS(f"    -> {created} question(s) saved as pending_review"))

            except Exception as e:
                gen_chunk.status = "FAILED"
                gen_chunk.error_message = str(e)
                gen_chunk.save(update_fields=["status", "error_message", "updated_at"])
                total_failed += 1
                self.stdout.write(self.style.WARNING(f"    -> FAILED: {e}"))

            time.sleep(sleep_seconds)

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. {total_generated} question(s) generated across "
                f"{len(pending_chunks) - total_failed} chunk(s), {total_failed} chunk(s) failed."
            )
        )
        if total_failed:
            self.stdout.write("Re-run the same command to retry FAILED chunks automatically (unless --retry-failed changes matter to you, this reruns them by default only if you pass --retry-failed).")

    def _call_with_retry(self, client, prompt, max_retries=3):
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
                    self.stdout.write("    429 hit — sleeping 20s and retrying...")
                    time.sleep(20)
                    continue
                if ("503" in error_str or "UNAVAILABLE" in error_str) and attempt < max_retries - 1:
                    self.stdout.write("    503 overloaded — sleeping 10s and retrying...")
                    time.sleep(10)
                    continue
                break
        raise RuntimeError(f"Generation failed after {max_retries} attempts: {last_error}")