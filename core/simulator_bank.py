"""
Pre-generation of the simulator question bank.

Source order per topic:  published lecture  ->  non-empty slide topic chunk  ->  nothing
(a topic with neither is marked NO_SOURCE and shown as "coming soon").

There is deliberately NO keyword fallback into the raw slide text. An empty
or missing chunk must never turn into questions written from general
knowledge.
"""
import hashlib
import json
import re
import time

from django.db import connection, transaction
from google.genai import types

from .models import (
    CourseDefinition, PreGeneratedLesson, SlideTopicChunk,
    SimulatorQuestion, SimulatorBankTopic,
)
from .simulator_bank_prompts import (
    BANK_MCQ_PROMPT, BANK_THEORY_PROMPT, BANK_VERIFIER_PROMPT,
    SOURCE_NOTE_LECTURE, SOURCE_NOTE_SLIDE,
)

MODEL_NAME = "gemini-3.6-flash"
MAX_SOURCE_CHARS = 24000
CALL_GAP_SECONDS = 3.5

MCQ_MAX_TOKENS = 24000
LONG_MAX_TOKENS = 32000
VERIFIER_MAX_TOKENS = 16000

MCQ_TO_GENERATE = 10       # generate extra, keep only those that pass verification
LONG_TO_GENERATE = 4
MIN_MCQ_TO_SAVE = 4        # below this the topic is marked FAILED, not half-saved
MIN_LONG_TO_SAVE = 2
MAX_RECALL_SHARE = 0.2


# ─── Source resolution ────────────────────────────────────────────────────────

def _cap_source(text):
    text = text.strip()
    if len(text) <= MAX_SOURCE_CHARS:
        return text
    cut = text.rfind("\n\n", 0, MAX_SOURCE_CHARS)
    return text[:cut] if cut > 0 else text[:MAX_SOURCE_CHARS]


def resolve_topic_source(course_code, level, topic_name):
    """Returns (source_type, text). source_type is '' when there is nothing."""
    lesson = (
        PreGeneratedLesson.objects
        .filter(course__course_code=course_code, topic_title=topic_name,
                is_published=True, is_truncated=False)
        .exclude(content_chunk="")
        .order_by("-updated_at")
        .first()
    )
    if lesson and lesson.content_chunk.strip():
        return "lecture", _cap_source(lesson.content_chunk)

    chunk = (
        SlideTopicChunk.objects
        .filter(slide__course_code=course_code, slide__level=level, slide__parsed=True,
                topic_name=topic_name, is_empty=False)
        .order_by("-updated_at")
        .first()
    )
    if chunk and chunk.chunk_text.strip():
        return "slide", _cap_source(chunk.chunk_text)

    return "", ""


# ─── Model plumbing ───────────────────────────────────────────────────────────

def _render(template, values):
    """Single-pass placeholder fill: inserted text is never re-scanned."""
    return re.sub(r"__([A-Z_]+)__", lambda m: values.get(m.group(1), m.group(0)), template)


def _call_model(prompt, max_tokens, temperature, label, attempts=5):
    from .views import simulator_client   # lazy: avoids a circular import
    last_error = None
    for attempt in range(attempts):
        try:
            response = simulator_client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=max_tokens, temperature=temperature,
                ),
            )
            text = response.text
            if not text or not text.strip():
                raise ValueError("Empty response")
            hit_limit = False
            try:
                hit_limit = "MAX_TOKENS" in str(response.candidates[0].finish_reason)
            except Exception:
                pass
            time.sleep(CALL_GAP_SECONDS)
            return text, hit_limit
        except Exception as e:
            last_error = e
            s = str(e)
            rate_limited = "429" in s or "RESOURCE_EXHAUSTED" in s
            transient = any(m in s for m in (
                "503", "UNAVAILABLE", "timeout", "Timeout",
                "ConnectionError", "RemoteDisconnected",
            ))
            if (rate_limited or transient) and attempt < attempts - 1:
                wait = min(20 * (attempt + 1), 90) if rate_limited else min(15 * (2 ** attempt), 120)
                print(f"{label}: {'rate limit' if rate_limited else 'server busy'} "
                      f"(attempt {attempt + 1}/{attempts}) — waiting {wait}s...")
                time.sleep(wait)
                continue
            break
    raise RuntimeError(f"{label} failed: {last_error}")


# ─── Cleaning / structural validation ─────────────────────────────────────────

_LETTER_PREFIX = re.compile(r"^[A-Da-d][\.\)]\s*")
_LEVELS = ("recall", "application", "analysis")


def _clean_mcq(raw):
    if not isinstance(raw, dict):
        return None, "not an object"
    question = str(raw.get("question", "")).strip()
    options = raw.get("options")
    if not question or not isinstance(options, list) or len(options) != 4:
        return None, "needs a stem and exactly 4 options"
    options = [_LETTER_PREFIX.sub("", str(o)).strip() for o in options]
    if any(not o for o in options) or len({o.lower() for o in options}) != 4:
        return None, "empty or duplicate options"
    try:
        correct_index = int(raw.get("correct_index"))
    except (TypeError, ValueError):
        return None, "bad correct_index"
    if not 0 <= correct_index <= 3:
        return None, "correct_index out of range"
    explanation = str(raw.get("explanation", "")).strip()
    if not explanation:
        return None, "missing explanation"
    level = raw.get("cognitive_level")
    return {
        "question_type": "mcq",
        "cognitive_level": level if level in _LEVELS else "application",
        "marks": 2,
        "content": {
            "question": question, "options": options,
            "correct_index": correct_index, "explanation": explanation,
        },
    }, ""


def _clean_long(raw):
    if not isinstance(raw, dict):
        return None, "not an object"
    question = str(raw.get("question", "")).strip()
    answer = str(raw.get("model_answer", "")).strip()
    if not question or not answer:
        return None, "missing question or model answer"
    qtype = raw.get("type") if raw.get("type") in ("theory", "calculation") else "theory"
    try:
        marks = int(raw.get("marks"))
    except (TypeError, ValueError):
        return None, "bad marks"
    if not 5 <= marks <= 20:
        return None, "marks out of range"

    scheme_raw = raw.get("marking_scheme")
    if not isinstance(scheme_raw, list) or not scheme_raw:
        return None, "missing marking scheme"
    scheme, total = [], 0.0
    for item in scheme_raw:
        if not isinstance(item, dict):
            return None, "bad marking scheme item"
        point = str(item.get("point", "")).strip()
        try:
            m = float(item.get("marks"))
        except (TypeError, ValueError):
            return None, "bad marking scheme marks"
        if not point or m <= 0:
            return None, "bad marking scheme item"
        scheme.append({"point": point, "marks": int(m) if m == int(m) else m})
        total += m
    if abs(total - marks) > 1e-6:
        return None, f"marking scheme sums to {total:g}, not {marks}"

    level = raw.get("cognitive_level")
    return {
        "question_type": qtype,
        "cognitive_level": level if level in _LEVELS else "application",
        "marks": marks,
        "content": {"question": question, "model_answer": answer, "marking_scheme": scheme},
    }, ""


def _cap_recall(items):
    """Enforce in code, not just in the prompt, that recall stays a minority."""
    limit = max(1, int(len(items) * MAX_RECALL_SHARE))
    kept, recall = [], 0
    for it in items:
        if it["cognitive_level"] == "recall":
            if recall >= limit:
                continue
            recall += 1
        kept.append(it)
    return kept


# ─── Verification ─────────────────────────────────────────────────────────────

def _parse_verdicts(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("verifier returned no JSON")
    data = json.loads(text[start:end + 1])
    out = {}
    for r in data.get("results") or []:
        try:
            out[int(r.get("id"))] = (
                r.get("verdict") == "pass",
                [str(x) for x in (r.get("issues") or [])],
            )
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def _verify(kind, source_text, items):
    payload = []
    for i, it in enumerate(items):
        c = it["content"]
        if kind == "mcq":
            payload.append({
                "id": i, "question": c["question"], "options": c["options"],
                "correct_index": c["correct_index"], "explanation": c["explanation"],
            })
        else:
            payload.append({
                "id": i, "type": it["question_type"], "marks": it["marks"],
                "question": c["question"], "model_answer": c["model_answer"],
                "marking_scheme": c["marking_scheme"],
            })
    prompt = _render(BANK_VERIFIER_PROMPT, {
        "KIND": "multiple choice" if kind == "mcq" else "theory / calculation",
        "SOURCE": source_text,
        "QUESTIONS": json.dumps(payload, ensure_ascii=False, indent=1),
    })
    text, hit = _call_model(prompt, VERIFIER_MAX_TOKENS, 0.0, "Question verifier")
    if hit:
        raise RuntimeError("verifier hit its output limit")
    return _parse_verdicts(text)


# ─── Generate + verify one kind of question ───────────────────────────────────

def _make_verified(kind, ctx, notes):
    from .views import _repair_and_parse_json_array   # lazy: avoids a circular import

    is_mcq = kind == "mcq"
    n = MCQ_TO_GENERATE if is_mcq else LONG_TO_GENERATE

    for attempt in range(2):
        prompt = _render(BANK_MCQ_PROMPT if is_mcq else BANK_THEORY_PROMPT, {
            "LEVEL": ctx["level"],
            "COURSE_CODE": ctx["course_code"],
            "COURSE_TITLE": ctx["course_title"],
            "TOPIC": ctx["topic"],
            "SOURCE_KIND_NOTE": SOURCE_NOTE_LECTURE if ctx["source_type"] == "lecture" else SOURCE_NOTE_SLIDE,
            "N": str(n),
            "SOURCE": ctx["source_text"],
        })
        text, hit = _call_model(
            prompt, MCQ_MAX_TOKENS if is_mcq else LONG_MAX_TOKENS,
            0.5, f"{kind} generation — {ctx['topic']}",
        )
        if not hit:
            break
        n = max(6 if is_mcq else 3, n * 2 // 3)   # retry once with fewer questions
    else:
        raise RuntimeError(f"{kind} generation hit the output limit twice — nothing saved")

    raw = _repair_and_parse_json_array(text)
    if not isinstance(raw, list):
        raise ValueError(f"{kind} generation did not return a JSON array")

    cleaner = _clean_mcq if is_mcq else _clean_long
    items = []
    for r in raw:
        item, reason = cleaner(r)
        if item:
            items.append(item)
        else:
            notes.append(f"{kind}: dropped malformed question ({reason})")
    if is_mcq:
        items = _cap_recall(items)
    if not items:
        return []

    verdicts = _verify(kind, ctx["source_text"], items)
    passed = []
    for i, item in enumerate(items):
        ok, issues = verdicts.get(i, (False, ["verifier gave no verdict"]))
        if ok:
            passed.append(item)
        else:
            notes.append(f"{kind} rejected: " + "; ".join(issues)[:200])
    return passed


# ─── One topic ────────────────────────────────────────────────────────────────

def _fresh_connection():
    """Model calls can take minutes. A pooled database connection (Supabase)
    may be dropped while idle, so reconnect before writing."""
    if not connection.in_atomic_block:
        connection.close()


def process_topic(course_code, course_title, level, topic,
                  force=False, auto_approve=False, log=print):
    """Returns 'completed' | 'skipped' | 'no_source' | 'failed'."""
    row, _ = SimulatorBankTopic.objects.get_or_create(
        course_code=course_code, level=level, topic_name=topic,
    )
    source_type, source_text = resolve_topic_source(course_code, level, topic)

    if not source_type:
        row.status = "NO_SOURCE"
        row.source_type = ""
        row.error_message = "No published lecture and no non-empty slide chunk for this topic."
        row.save()
        log(f"  – {topic}: no source yet (coming soon)")
        return "no_source"

    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    if not force and row.status == "COMPLETED" and row.source_hash == source_hash:
        log(f"  = {topic}: unchanged, skipped")
        return "skipped"

    ctx = {
        "level": level, "course_code": course_code, "course_title": course_title,
        "topic": topic, "source_type": source_type, "source_text": source_text,
    }
    notes = []
    try:
        mcqs = _make_verified("mcq", ctx, notes)
        longs = _make_verified("long", ctx, notes)
    except Exception as e:
        _fresh_connection()
        row.status = "FAILED"
        row.error_message = str(e)[:1000]
        row.save()
        log(f"  ✗ {topic}: {e}")
        return "failed"
    _fresh_connection()

    if len(mcqs) < MIN_MCQ_TO_SAVE and len(longs) < MIN_LONG_TO_SAVE:
        row.status = "FAILED"
        row.error_message = (
            f"Too few questions survived verification "
            f"(MCQ {len(mcqs)}/{MIN_MCQ_TO_SAVE}, written {len(longs)}/{MIN_LONG_TO_SAVE}). "
            + " | ".join(notes[:6])
        )[:1000]
        row.save()
        log(f"  ✗ {topic}: {row.error_message}")
        return "failed"

    status = "approved" if auto_approve else "pending_review"
    with transaction.atomic():
        SimulatorQuestion.objects.filter(
            course_code=course_code, level=level, topic_name=topic,
        ).exclude(status="retired").update(status="retired")
        for item in mcqs + longs:
            SimulatorQuestion.objects.create(
                course_code=course_code, level=level, topic_name=topic,
                question_type=item["question_type"],
                cognitive_level=item["cognitive_level"],
                marks=item["marks"], content=item["content"],
                source_type=source_type, source_hash=source_hash,
                status=status, verified=True,
            )
        row.status = "COMPLETED"
        row.source_type = source_type
        row.source_hash = source_hash
        row.mcq_saved = len(mcqs)
        row.long_saved = len(longs)
        row.error_message = " | ".join(notes[:6])[:1000]
        row.save()

    log(f"  ✓ {topic}: {len(mcqs)} MCQ + {len(longs)} written saved from {source_type} ({status})")
    return "completed"


# ─── One course ───────────────────────────────────────────────────────────────

def run_bank_generation(course_code, level, topics=None, force=False,
                        auto_approve=False, limit=None, log=print):
    """Generate the bank for every topic in the course's lecture topic list
    (same list the lectures use). Safe to re-run: finished topics whose
    source hasn't changed are skipped. `limit` caps API-consuming topics
    (completed + failed) per run."""
    from .views import _get_total_topics_for_course

    course = CourseDefinition.objects.filter(course_code=course_code).first()
    course_title = course.course_title if course else course_code

    targets = [t for t in _get_total_topics_for_course(course_code, level)
               if not topics or t in topics]
    summary = {"completed": 0, "skipped": 0, "no_source": 0, "failed": 0}
    log(f"{course_code} ({level}L): {len(targets)} topic(s)")

    used = 0
    for topic in targets:
        if limit is not None and used >= limit:
            log("Limit reached — run again to continue.")
            break
        outcome = process_topic(course_code, course_title, level, topic,
                                force=force, auto_approve=auto_approve, log=log)
        summary[outcome] += 1
        if outcome in ("completed", "failed"):
            used += 1
    return summary


# ─── For the setup page ───────────────────────────────────────────────────────

def get_topic_availability(course_code, level):
    """Every topic in the lecture topic list, with approved-question counts.
    ready=False means the picker should show 'coming soon'."""
    from django.db.models import Count, Q
    from .views import _get_total_topics_for_course

    rows = (
        SimulatorQuestion.objects
        .filter(course_code=course_code, level=level, status="approved", verified=True)
        .values("topic_name")
        .annotate(mcq=Count("id", filter=Q(question_type="mcq")),
                  written=Count("id", filter=~Q(question_type="mcq")))
    )
    counts = {r["topic_name"]: (r["mcq"], r["written"]) for r in rows}

    out = []
    for topic in _get_total_topics_for_course(course_code, level):
        mcq, written = counts.get(topic, (0, 0))
        out.append({"topic": topic, "mcq": mcq, "written": written,
                    "ready": (mcq + written) > 0})
    return out