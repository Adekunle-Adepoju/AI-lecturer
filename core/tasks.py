import logging
import os

from django.db import close_old_connections

logger = logging.getLogger(__name__)

# One battle job handles at most this many chunks so it finishes in time.
# Click the button again to continue; finished chunks are skipped.
BATTLE_CHUNKS_PER_JOB = 25

def _queue_quiz_for(course_id, week_number, topic):
    """Queue a quiz for this lesson unless it already has a current one."""
    from django_q.tasks import async_task
    from .models import PreGeneratedLesson
    from .views import _lesson_quiz_is_current
    lesson = PreGeneratedLesson.objects.filter(
        course_id=course_id, week_number=week_number, topic_title=topic,
    ).select_related("course").first()
    if lesson and lesson.content_chunk.strip() and not lesson.is_truncated and not _lesson_quiz_is_current(lesson):
        async_task(
            "core.tasks.generate_lesson_quiz_task", lesson.id,
            task_name=f"Quiz {lesson.course.course_code} W{week_number}: {topic[:60]}",
        )


def generate_lesson_quiz_task(lesson_id, force=False):
    _fresh_db()
    from .views import generate_lesson_quiz
    return generate_lesson_quiz(lesson_id, force)

def _slide_report(slide, headline, diagram_failed=None):
    """One honest line for the Background jobs page. Starts with OK or WARNING."""
    from .slide_topic_extractor import summarize_slide
    s = summarize_slide(slide)
    problems = []
    if diagram_failed:
        shown = ", ".join(str(n) for n in diagram_failed[:15]) + ("..." if len(diagram_failed) > 15 else "")
        problems.append(f"{len(diagram_failed)} picture page(s) not described (pages {shown}) - click 'Retry diagrams'")
    if s["unplaced_pages"]:
        problems.append(f"{len(s['unplaced_pages'])} content page(s) in no topic ({', '.join(s['unplaced_pages'][:10])})")
    if s["empty_topics"]:
        problems.append(f"{len(s['empty_topics'])} topic(s) with no content - click 'Run split'")
    if s["topics_incomplete"]:
        problems.append("topic extraction incomplete - click 'Retry topics'")
    if s["cleanup_fallbacks"]:
        problems.append(f"{s['cleanup_fallbacks']} text chunk(s) not cleaned - click 'Retry cleanup'")
    if s["split_failed"]:
        problems.append(f"{s['split_failed']} split step(s) failed - click 'Run split'")

    base = f"{slide.course_code}: {headline}, {s['topics']} topics"
    if problems:
        return "WARNING: " + base + ". Needs attention: " + "; ".join(problems) + "."
    return "OK: " + base + ", every content page placed."

def _fresh_db():
    # Replaces database connections that went stale during a long Gemini call.
    close_old_connections()


# ─── Lessons ──────────────────────────────────────────────────────────────────

def _outline_reference(course):
    from .models import CourseOutline
    try:
        outline = CourseOutline.objects.get(
            course_code=course.course_code, level=course.level, parsed=True,
        )
        if outline.extracted_text:
            return f"\n\nCOURSE OUTLINE REFERENCE:\n{outline.extracted_text[:2000]}"
    except CourseOutline.DoesNotExist:
        pass
    return ""


def pregenerate_topic_lesson(course_id, week_number, topic, topic_index, total_topics):
    """Generates the lesson for ONE topic."""
    _fresh_db()
    from .models import CourseDefinition, PreGeneratedLesson
    from .views import (
        _find_slide_content_for_topic, _generate_verified_lecture,
        _render_reference_table_lecture, generation_client,
    )
    from .slide_topic_extractor import is_reference_table_content
    from .lecture_completeness import (
        MAX_CONTINUATION_ATTEMPTS, continue_truncated_lecture,
        detect_likely_duplicate_reteach, find_safe_cutoff, is_lecture_truncated,
    )

    course = CourseDefinition.objects.get(pk=course_id)
    from .outline_generation import (
        NO_SOURCE_MESSAGE, SOURCE_OUTLINE, get_generation_source, pregenerate_outline_lesson,
    )
    source = get_generation_source(course)
    if source is None:
        raise RuntimeError(NO_SOURCE_MESSAGE)
    if source == SOURCE_OUTLINE:
        return pregenerate_outline_lesson(course, week_number, topic, topic_index, total_topics)
    chunk_text = _find_slide_content_for_topic(course.course_code, course.level, topic)
    slide_text = (
        f"\n\nLECTURER SLIDES (focus only on content relevant to this topic):\n{chunk_text}"
        if chunk_text else ""
    )

    existing = PreGeneratedLesson.objects.filter(
        course=course, week_number=week_number, topic_title=topic,
    ).first()
    if existing and existing.source_type == "outline":
        existing = None   # slides now exist: regenerate from them

    # the "already generated" skip, so re-running Generate backfills missing quizzes
    if existing and existing.content_chunk.strip() and not existing.is_truncated:
        _queue_quiz_for(course.id, week_number, topic)
        return f"skipped (already generated): {topic}"

    # Reference-table topics need no AI call
    if chunk_text and is_reference_table_content(chunk_text):
        PreGeneratedLesson.objects.update_or_create(
            course=course, week_number=week_number, topic_title=topic, source_type="slides", review_note="",
            defaults={
                "content_chunk": _render_reference_table_lecture(topic, chunk_text),
                "is_published": False, "is_truncated": False, "continuation_attempts": 0,
            },
        )
        return f"saved reference-table lesson: {topic}"

    # Lecture was cut off earlier -> continue it
    if existing and existing.is_truncated and existing.content_chunk.strip():
        if existing.continuation_attempts >= MAX_CONTINUATION_ATTEMPTS:
            raise RuntimeError(
                f"{topic}: still incomplete after {MAX_CONTINUATION_ATTEMPTS} attempts "
                f"— needs manual review/edit in admin."
            )
        continuation = continue_truncated_lecture(
            generation_client, course.course_code, course.course_title,
            topic, week_number, course.level,
            student_name="Student", topic_index=topic_index,
            previous_content=existing.content_chunk,
            slide_text=slide_text + _outline_reference(course),
            total_topics_in_round=total_topics,
        )
        if detect_likely_duplicate_reteach(existing.content_chunk, continuation):
            raise RuntimeError(
                f"{topic}: continuation re-taught an earlier section — discarded. "
                f"Needs manual review or fresh regeneration."
            )
        merged = existing.content_chunk.rstrip() + "\n\n" + continuation.strip()
        still_truncated = is_lecture_truncated(continuation)
        existing.content_chunk = find_safe_cutoff(merged) if still_truncated else merged
        existing.is_truncated = still_truncated
        existing.continuation_attempts += 1
        existing.save(update_fields=["content_chunk", "is_truncated", "continuation_attempts"])
        if still_truncated:
            return f"PARTIAL: {topic} still truncated — run again to continue."
        _queue_quiz_for(course.id, week_number, topic)
        return f"saved (continued): {topic}"

    # Brand new lesson
    if not chunk_text:
        raise RuntimeError(f"{topic}: no slide content found — run the topic split first.")

    full_text, review_note, hit_limit = _generate_verified_lecture(
        course.course_code, course.course_title, topic, chunk_text,
        is_course_opening=(week_number == 1 and topic_index == 0),
        course_context=(
            f"Level: {course.get_level_display()}\n"
            f"Department: {course.get_department_display()}\n"
        ),
    )
    if hit_limit:
        raise RuntimeError(f"{topic}: hit the output limit — not saved. Run again.")
    if not full_text or not full_text.strip():
        raise RuntimeError(f"{topic}: AI returned an empty response.")

    _fresh_db()
    PreGeneratedLesson.objects.update_or_create(
        course=course, week_number=week_number, topic_title=topic, source_type="slides", review_note="",
        defaults={
            "content_chunk": full_text, "is_published": False,
            "is_truncated": False, "continuation_attempts": 0,
        },
    )
    _queue_quiz_for(course.id, week_number, topic)
    if review_note:
        return f"saved: {topic} — REVIEW BEFORE PUBLISHING: {review_note}"
    return f"saved: {topic}"


# ─── Slide pipeline ───────────────────────────────────────────────────────────

def parse_slide_upload(new_slide_id, existing_slide_id=None):
    """Reads the PDF, cleans it, extracts topics.
    If existing_slide_id is given, the new upload is a temporary record that
    gets merged into the existing deck and then deleted."""
    _fresh_db()
    from .models import SlideDocument
    from .slide_topic_extractor import _parse_slide_document

    if existing_slide_id is None:
        slide = SlideDocument.objects.get(pk=new_slide_id)
        stats = _parse_slide_document(slide)
        return _slide_report(slide, "slide processed", stats["diagram_failed"])

    temp = SlideDocument.objects.get(pk=new_slide_id)
    existing = SlideDocument.objects.get(pk=existing_slide_id)
    try:
        stats = _parse_slide_document(temp)
        _fresh_db()
        existing.refresh_from_db()
        existing.extracted_text = (
            f"{existing.extracted_text}\n\n--- [Slide Continuation] ---\n\n{temp.extracted_text}"
        )
        known = set(existing.extracted_topics)
        new_topics = [t for t in temp.extracted_topics if t not in known]
        existing.extracted_topics.extend(new_topics)
        existing.append_count += 1
        existing.save()
        msg = f"Appended to {existing.course_code}: {len(new_topics)} new topics. Run the topic split to include them."
        if stats["diagram_failed"]:
            return (f"WARNING: {msg} {len(stats['diagram_failed'])} picture page(s) of the new file were "
                    f"not described, and appended decks cannot be retried - upload that file again later.")
        return msg
    finally:
        try:
            if temp.file and os.path.isfile(temp.file.path):
                os.remove(temp.file.path)
        except OSError:
            pass
        temp.delete()


def retry_topic_extraction(slide_id):
    _fresh_db()
    from .models import SlideDocument
    from .slide_topic_extractor import extract_topics_for_slide_resumable

    slide = SlideDocument.objects.get(pk=slide_id)
    topics, incomplete = extract_topics_for_slide_resumable(slide)
    msg = f"{slide.course_code}: {len(topics)} topics found"
    if incomplete:
        raise RuntimeError(msg + " — some chunks still failed; run again to resume.")
    return msg


def retry_cleanup(slide_id):
    _fresh_db()
    from .models import SlideDocument
    from .slide_topic_extractor import _cleanup_mangled_text_with_ai

    slide = SlideDocument.objects.get(pk=slide_id)
    reset_count = slide.cleanup_chunks.exclude(error_message="").update(
        status="PENDING", error_message="",
    )
    cleaned = _cleanup_mangled_text_with_ai(
        slide.course_code, slide.course_title, slide.extracted_text, slide=slide,
    )
    slide.extracted_text = cleaned
    slide.save(update_fields=["extracted_text"])

    still_failed = slide.cleanup_chunks.exclude(error_message="").count()
    msg = f"{slide.course_code}: cleanup reprocessed {reset_count} chunk(s)"
    if still_failed:
        raise RuntimeError(f"{msg}; {still_failed} still fell back to raw text — run again.")
    if reset_count:
        msg += " (topics / topic split may now be out of date — re-run them)"
    return msg


def split_weeks_by_topic(slide_id):
    _fresh_db()
    from .models import SlideDocument
    from .slide_topic_extractor import split_all_weeks_by_topic

    slide = SlideDocument.objects.get(pk=slide_id)
    split_all_weeks_by_topic(slide)
    count = slide.topic_chunks.exclude(is_empty=True).count()
    return f"{slide.course_code}: {count} non-empty topic chunks saved"


def run_topic_split(slide_id):
    _fresh_db()
    from .models import SlideDocument
    from .slide_topic_extractor import (
        retry_missing_topic_splits, split_slide_by_extracted_topics,
    )

    slide = SlideDocument.objects.get(pk=slide_id)
    if not slide.extracted_topics or not slide.extracted_text.strip():
        raise RuntimeError("No extracted topics or text saved for this slide.")

    _success, count = split_slide_by_extracted_topics(slide)
    empty = list(slide.topic_chunks.filter(is_empty=True).values_list("topic_name", flat=True))
    if empty:
        _fresh_db()
        retry_missing_topic_splits(slide, topic_names=empty)
        still_empty = list(
            slide.topic_chunks.filter(is_empty=True).values_list("topic_name", flat=True)
        )
        if still_empty:
            raise RuntimeError(
                f"{slide.course_code}: {count} topics saved, but {len(still_empty)} still "
                f"empty: {', '.join(still_empty[:5])}. Run again to retry."
            )
    return _slide_report(slide, "topic split done")


# ─── Outline / past questions ─────────────────────────────────────────────────

def parse_outline(outline_id):
    _fresh_db()
    from .models import CourseOutline
    from .outline_parser import _parse_course_outline

    outline = CourseOutline.objects.get(pk=outline_id)
    _parse_course_outline(outline)
    outline.refresh_from_db()
    if not outline.topics_json:
        raise RuntimeError(f"{outline.course_code}: parser ran but found no topics.")
    return f"{outline.course_code}: outline parsed"


def parse_past_questions(pq_id):
    _fresh_db()
    from .models import PastQuestion
    from .past_question_parser import _parse_past_question_file

    pq = PastQuestion.objects.get(pk=pq_id)
    _parse_past_question_file(pq)
    return f"{pq.course_code}: {len(pq.parsed_questions)} questions extracted"


# ─── Battle + simulator bank ──────────────────────────────────────────────────

def generate_battle_questions(course_code=None, retry_failed=False, limit=None):
    _fresh_db()
    from battle.generation import run_battle_question_generation

    capped = min(limit, BATTLE_CHUNKS_PER_JOB) if limit else BATTLE_CHUNKS_PER_JOB
    logs = []
    result = run_battle_question_generation(
        per_chunk=8, sleep_seconds=3.5,
        course_filter=course_code or None,
        retry_failed=retry_failed, limit=capped, log=logs.append,
    )
    ok = result["chunks_processed"] - result["chunks_failed"]
    msg = f"{result['questions_generated']} question(s) from {ok} chunk(s) (pending_review)"
    if result["failures"]:
        raise RuntimeError(msg + " | failures: " + "; ".join(result["failures"][:5]))
    return msg


def generate_sim_topic(course_code, topic, force=False):
    _fresh_db()
    from .models import CourseDefinition
    from .simulator_bank import process_topic

    course = CourseDefinition.objects.get(course_code=course_code)
    logs = []
    outcome = process_topic(
        course.course_code, course.course_title, course.level, topic,
        force=force, log=logs.append,
    )
    return " ".join(l.strip() for l in logs) or str(outcome)

def retry_diagrams(slide_id):
    """Describe only the picture pages that are still missing, then rebuild the topic split."""
    _fresh_db()
    from .models import SlideDocument
    from .slide_topic_extractor import retry_missing_diagrams, split_slide_by_extracted_topics

    slide = SlideDocument.objects.get(pk=slide_id)
    added, still_missing = retry_missing_diagrams(slide)
    if added:
        _fresh_db()
        # The text changed, so the split rows are stale; this rebuilds them automatically.
        split_slide_by_extracted_topics(slide)
        slide.refresh_from_db()
    return _slide_report(slide, f"{added} diagram(s) added", still_missing)