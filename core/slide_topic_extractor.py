import json
import time
import re
from google import genai
from google.genai import types
from django.conf import settings
import math
from .models import SlideDocument, SlideExtractionChunk, SlideTopicChunk, CourseOutline


def _get_batch_client():
    return genai.Client(
        api_key=settings.GEMINI_API_KEY_EXTRACTION,
        http_options=types.HttpOptions(timeout=60_000),  # ms — raises instead of hanging forever
    )


DEFAULT_MODEL_FALLBACK_CHAIN = ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"]


def _call_gemini_with_retry(client, model, contents, config, course_code, chunk_label, max_retries=3, model_fallback_chain=None):
    """Calls Gemini with 429-aware retry. If a model is exhausted (all
    retries fail with 429/503), automatically moves to the next model in
    model_fallback_chain and resumes retrying there — instead of giving
    up entirely. Cycles through the whole chain once; only returns None
    if EVERY model in the chain fails.

    `model` is treated as the starting point in the chain if it appears
    there; otherwise it's tried first, then the full chain is attempted.
    Returns response.text on success, or None if every model failed."""
    chain = model_fallback_chain or DEFAULT_MODEL_FALLBACK_CHAIN

    # Build the actual order to try: start at `model` if it's in the
    # chain, otherwise try `model` first then the whole chain.
    if model in chain:
        start_idx = chain.index(model)
        models_to_try = chain[start_idx:] + chain[:start_idx]
    else:
        models_to_try = [model] + [m for m in chain if m != model]

    for model_idx, current_model in enumerate(models_to_try):
        is_last_model = (model_idx == len(models_to_try) - 1)

        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model=current_model,
                    contents=contents,
                    config=config,
                )
                if model_idx > 0:
                    print(f"[{course_code}] {chunk_label}: succeeded on fallback model {current_model}.")
                return response.text
            except Exception as e:
                error_str = str(e)
                is_rate_limit = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
                is_unavailable = "503" in error_str or "UNAVAILABLE" in error_str

                if is_rate_limit or is_unavailable:
                    print(f"[{course_code}] {chunk_label}: {current_model} — 429/503 (attempt {attempt + 1}/{max_retries}) — sleeping 20s...")
                    time.sleep(20)
                    continue
                else:
                    print(f"[{course_code}] {chunk_label}: {current_model} — non-rate-limit error ({e}) — attempt {attempt + 1}/{max_retries}")
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        continue
                    break  # non-rate-limit error, exhausted retries on this model

        # This model is exhausted (all retries failed) — move to next model.
        if not is_last_model:
            print(f"[{course_code}] {chunk_label}: {current_model} exhausted — falling back to next model.")
        else:
            print(f"[{course_code}] {chunk_label}: {current_model} exhausted — no more fallback models, giving up.")

    return None

def _get_or_create_extraction_chunks(slide):
    """Ensure SlideExtractionChunk rows exist for this slide's macro-chunks.
    Only creates them once — on later calls (retries), returns the existing
    rows unchanged, so chunk boundaries and prior progress are preserved."""
    existing = list(slide.extraction_chunks.all())
    if existing:
        return existing

    chunks_text = _macro_chunk_text(slide.extracted_text)
    created = []
    for idx, chunk_text in enumerate(chunks_text):
        obj = SlideExtractionChunk.objects.create(
            slide=slide,
            chunk_index=idx,
            chunk_text=chunk_text,
            status="PENDING",
        )
        created.append(obj)
    return created


def extract_topics_for_slide_resumable(slide):
    """Extract topics for a SlideDocument, resuming from previously
    completed macro-chunks instead of reprocessing the whole document.
    Only chunks with status != COMPLETED are sent to the model. Saves
    slide.extracted_topics / slide.topics_incomplete directly.
    Returns (topics, incomplete)."""

    batch_client = _get_batch_client()

    chunk_rows = _get_or_create_extraction_chunks(slide)
    pending_rows = [c for c in chunk_rows if c.status != "COMPLETED"]

    if not pending_rows:
        print(f"[{slide.course_code}] All {len(chunk_rows)} chunks already completed — nothing to reprocess.")

    for chunk_row in pending_rows:
        chunk_label = f"Chunk {chunk_row.chunk_index + 1}/{len(chunk_rows)}"
        chunk_text = chunk_row.chunk_text

        if not chunk_text.strip():
            chunk_row.status = "COMPLETED"
            chunk_row.extracted_topics = []
            chunk_row.save(update_fields=["status", "extracted_topics", "updated_at"])
            continue

        raw = _call_gemini_with_retry(
            client=batch_client,
            model="gemini-3.6-flash",
            contents=(
                f"Course: {slide.course_code} — {slide.course_title}\n\n"
                f"Slide text chunk {chunk_row.chunk_index + 1} of {len(chunk_rows)}:\n{chunk_text}"
            ),
            config=types.GenerateContentConfig(
                system_instruction=(
                    "You are extracting topic names from university lecture slide text. "
                    "Return ONLY a valid JSON array of topic name strings. "
                    "Each topic should be a concise title (3-8 words). "
                    "If no clear topics are found in this chunk, return exactly: [] "
                    "Do NOT return markdown, explanation, or any text outside the JSON array. "
                    "Example: [\"Introduction to Fluid Flow\", \"Darcy's Law\", \"Permeability Measurement\"]"
                ),
                max_output_tokens=2000,
            ),
            course_code=slide.course_code,
            chunk_label=chunk_label,
        )

        if raw is None:
            chunk_row.status = "FAILED"
            chunk_row.error_message = "Failed after retries (rate limit or API error)."
            chunk_row.save(update_fields=["status", "error_message", "updated_at"])
            time.sleep(3)
            continue

        if not raw.strip():
            chunk_row.status = "FAILED"
            chunk_row.error_message = "Empty response from model."
            chunk_row.save(update_fields=["status", "error_message", "updated_at"])
            time.sleep(3)
            continue

        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip()

        try:
            topics = json.loads(clean)
            if isinstance(topics, list):
                valid = [t for t in topics if isinstance(t, str) and t.strip()]
                # ── Mark COMPLETED immediately after a successful parse + save ──
                chunk_row.status = "COMPLETED"
                chunk_row.extracted_topics = valid
                chunk_row.error_message = ""
                chunk_row.save(update_fields=["status", "extracted_topics", "error_message", "updated_at"])
            else:
                chunk_row.status = "FAILED"
                chunk_row.error_message = "Model returned non-list JSON."
                chunk_row.save(update_fields=["status", "error_message", "updated_at"])
        except json.JSONDecodeError as e:
            chunk_row.status = "FAILED"
            chunk_row.error_message = f"JSON decode failed: {e}"
            chunk_row.save(update_fields=["status", "error_message", "updated_at"])

        time.sleep(3)

    # ── Aggregate topics from every COMPLETED chunk (old + newly retried) ──
    all_rows = slide.extraction_chunks.all()
    aggregated = []
    for row in all_rows:
        aggregated.extend(row.extracted_topics or [])

    seen = set()
    unique_topics = []
    for t in aggregated:
        if t not in seen:
            seen.add(t)
            unique_topics.append(t)

    incomplete = any(row.status != "COMPLETED" for row in all_rows)

    # ── Append onto whatever was already on the slide, never wipe it ──
    existing_topics = slide.extracted_topics or []
    merged = list(existing_topics)
    seen_merged = set(merged)
    for t in unique_topics:
        if t not in seen_merged:
            seen_merged.add(t)
            merged.append(t)

    slide.extracted_topics = merged
    slide.topics_incomplete = incomplete
    slide.save(update_fields=["extracted_topics", "topics_incomplete"])

    return merged, incomplete


def safe_extract_topics_from_slide(course_code, course_title, extracted_text):
    """Wrapper guaranteeing a (topics, incomplete) tuple even on total failure."""
    try:
        result = extract_topics_from_slide(course_code, course_title, extracted_text)
        if isinstance(result, tuple) and len(result) == 2:
            return result
        return (result if isinstance(result, list) else [], True)
    except Exception as e:
        print(f"[{course_code}] extract_topics_from_slide crashed entirely: {e}")
        return [], True

def _extract_page_text_layout_aware(page):
    """Try pdfplumber's layout-preserving mode first, which reconstructs
    column/row spacing using whitespace instead of dumping words in raw
    reading order. Falls back to plain extraction if layout mode returns
    nothing (rare, but happens on some malformed PDFs). Also pulls out
    any genuine tables separately, since extract_text() — even with
    layout=True — often still mangles true gridded tables (side-by-side
    comparison tables, flow-regime charts) worse than a dedicated table
    extractor does."""
    try:
        text = page.extract_text(layout=True, x_tolerance=2, y_tolerance=3)
    except Exception:
        text = None

    if not text or not text.strip():
        text = page.extract_text() or ""

    table_text = _extract_tables_as_markdown(page)
    if table_text:
        text = f"{text}\n\n{table_text}"

    return text


def _extract_tables_as_markdown(page):
    try:
        tables = page.extract_tables()
    except Exception:
        return ""

    if not tables:
        return ""

    blocks = []
    for table in tables:
        if not table or len(table) < 2:
            continue

        rows = [[(cell or "").strip().replace("\n", " ") for cell in row] for row in table]
        header = rows[0]
        body = rows[1:]

        if not any(h.strip() for h in header):
            continue

        # NEW — reject false-positive "tables" that are really just a
        # single bulleted text block pdfplumber misread as a 1-column
        # grid (common with PowerPoint-style bullet slides). A genuine
        # table has more than one column OR multiple short body rows;
        # a single wide column with one giant cell is bullet text.
        num_cols = len(header)
        if num_cols <= 1:
            continue
        # Also reject if every body row is basically one big blob
        # (a single non-empty cell per row, all in the same column) —
        # another signature of misdetected bullet lists.
        multi_cell_rows = sum(1 for r in body if sum(1 for c in r if c.strip()) > 1)
        if body and multi_cell_rows == 0:
            continue

        md_lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(["---"] * len(header)) + " |",
        ]
        for row in body:
            padded = (row + [""] * len(header))[:len(header)]
            md_lines.append("| " + " | ".join(padded) + " |")

        blocks.append("\n".join(md_lines))

    return "\n\n".join(blocks)

def _parse_slide_document(slide):
    """Called after upload — extracts text and topics, saves to SlideDocument."""
    import pdfplumber

    extracted_text = ""
    try:
        with pdfplumber.open(slide.file.path) as pdf:
            for page in pdf.pages:
                text = _extract_page_text_layout_aware(page)
                if text:
                    extracted_text += text + "\n"
    except Exception as e:
        print(f"PDF extraction failed for {slide.course_code}: {e}")

    extracted_text = _cleanup_mangled_text_with_ai(slide.course_code, slide.course_title, extracted_text, slide=slide)

    slide.extracted_text = extracted_text
    slide.parsed = True
    slide.save(update_fields=["extracted_text", "parsed"])

    if extracted_text.strip():
        try:
            extract_topics_for_slide_resumable(slide)
        except Exception as e:
            print(f"[{slide.course_code}] extraction crashed entirely: {e}")
            slide.topics_incomplete = True
            slide.save(update_fields=["topics_incomplete"])
        try:
            split_all_weeks_by_topic(slide)
        except Exception as e:
            print(f"[{slide.course_code}] topic-level split crashed entirely: {e}")
    else:
        slide.extracted_topics = []
        slide.topics_incomplete = False
        slide.save(update_fields=["extracted_topics", "topics_incomplete"])

def _macro_chunk_text(text, target_chunk_size=17500, min_chunks=3, max_chunks=None):
    """Split text into chunks close to target_chunk_size. No hard cap on
    chunk count by default — for very large decks (400k+ chars), forcing
    a low max_chunks previously produced megachunks Gemini's 8000-token
    output limit could never reproduce, causing cleanup/split to silently
    fall back to raw text on every chunk (see PGG434 upload). If
    max_chunks is explicitly passed, it still clamps the ceiling, but
    callers should only do that when they've checked it won't force
    oversized chunks on a large document."""
    total_len = len(text)

    if total_len <= target_chunk_size:
        return [text] if text.strip() else []

    num_chunks = math.ceil(total_len / target_chunk_size)
    num_chunks = max(min_chunks, num_chunks)
    if max_chunks:
        num_chunks = min(num_chunks, max_chunks)

    actual_chunk_size = math.ceil(total_len / num_chunks)
    chunks = [text[i:i + actual_chunk_size] for i in range(0, total_len, actual_chunk_size)]
    return [c for c in chunks if c.strip()]

CLEANUP_SYSTEM_INSTRUCTION = (
    "You are cleaning up raw text extracted from a university lecture slide "
    "deck PDF. The extraction process sometimes smashes side-by-side text "
    "columns together, mangles ASCII diagrams (flowcharts, flow-regime "
    "comparisons, process diagrams), or leaves oddly broken line spacing.\n\n"
    "Your job:\n"
    "1. If you see text that is clearly two columns merged into one broken "
    "stream (interleaved fragments from different columns), reconstruct it "
    "into two clear, separate, readable blocks or a Markdown table if the "
    "columns represent parallel comparisons.\n"
    "2. If you see a mangled ASCII diagram or flowchart (arrows, boxes, "
    "disconnected labels), restructure it into clean nested bullet points "
    "describing the flow/structure — do NOT attempt to redraw ASCII art, "
    "since it will re-mangle on re-extraction. Use bullets with indentation "
    "to represent hierarchy/sequence instead.\n"
    "3. If a Markdown table (| col | col |) is already present, leave it "
    "exactly as-is — it was already extracted cleanly by a dedicated table "
    "parser and does not need reformatting.\n"
    "4. Do NOT summarize, shorten, paraphrase, or omit any technical content, "
    "numbers, formulas, or terminology. Your only job is fixing STRUCTURE, "
    "never content. If a section already reads cleanly, copy it through "
    "completely unchanged.\n"
    "5. Do NOT add commentary, headers like 'Cleaned text:', or explanations. "
    "Return ONLY the cleaned text itself, nothing else — no markdown code "
    "fences wrapping the whole response.\n\n"
    "If the text has no structural problems at all, simply return it unchanged."
)

def _normalized_len(text):
    return len(re.sub(r'\s+', ' ', text).strip())

def _get_or_create_cleanup_chunks(slide, raw_text):
    """Ensure SlideCleanupChunk rows exist for this slide's macro-chunks.
    Only creates them once — on later calls (retries/resumes), returns the
    existing rows unchanged, so chunk boundaries and prior progress survive
    a crash, rate-limit, or manual interrupt."""
    from .models import SlideCleanupChunk

    existing = list(slide.cleanup_chunks.all())
    if existing:
        return existing

    chunks_text = _macro_chunk_text(raw_text)
    created = []
    for idx, chunk_text in enumerate(chunks_text):
        obj = SlideCleanupChunk.objects.create(
            slide=slide,
            chunk_index=idx,
            chunk_text=chunk_text,
            status="PENDING",
        )
        created.append(obj)
    return created


def _cleanup_mangled_text_with_ai(course_code, course_title, raw_text, slide=None):
    """Runs the raw extracted text through Gemini in macro-chunks to repair
    column-smashing and ASCII-diagram mangling. Resumable: each chunk's
    cleaned result is saved to SlideCleanupChunk immediately on success, so
    re-calling this after a rate-limit failure or crash skips chunks already
    COMPLETED and only spends API calls on what's left. Falls back to the
    raw chunk text for any chunk that ultimately fails, so a cleanup failure
    never loses content — it just leaves that portion un-prettified."""
    if not raw_text.strip():
        return raw_text

    if slide is None:
        # Backward-compatible fallback: no slide passed means no persistence,
        # behaves like the old non-resumable version.
        return _cleanup_mangled_text_no_resume(course_code, course_title, raw_text)

    client = _get_batch_client()
    chunk_rows = _get_or_create_cleanup_chunks(slide, raw_text)
    pending_rows = [c for c in chunk_rows if c.status != "COMPLETED"]

    if not pending_rows:
        print(f"[{course_code}] All {len(chunk_rows)} cleanup chunks already completed — nothing to reprocess.")

    for chunk_row in pending_rows:
        chunk_label = f"Cleanup chunk {chunk_row.chunk_index + 1}/{len(chunk_rows)}"
        chunk_text = chunk_row.chunk_text

        if not chunk_text.strip():
            chunk_row.status = "COMPLETED"
            chunk_row.cleaned_text = chunk_text
            chunk_row.save(update_fields=["status", "cleaned_text", "updated_at"])
            continue

        result = _call_gemini_with_retry(
            client=client,
            model="gemini-3.6-flash",
            contents=(
                f"Course: {course_code} — {course_title}\n\n"
                f"Raw extracted slide text (chunk {chunk_row.chunk_index + 1} of {len(chunk_rows)}):\n{chunk_text}"
            ),
            config=types.GenerateContentConfig(
                system_instruction=CLEANUP_SYSTEM_INSTRUCTION,
                max_output_tokens=8000,
            ),
            course_code=course_code,
            chunk_label=chunk_label,
        )

        if result and result.strip() and _normalized_len(result) >= _normalized_len(chunk_text) * 0.6:
            chunk_row.status = "COMPLETED"
            chunk_row.cleaned_text = result.strip()
            chunk_row.error_message = ""
            chunk_row.save(update_fields=["status", "cleaned_text", "error_message", "updated_at"])
        else:
            # Missing, empty, or suspiciously short — fall back to raw text
            # for THIS chunk, but still mark COMPLETED so a re-run doesn't
            # keep re-trying a chunk that's already been resolved (even if
            # resolved via fallback rather than a real cleanup).
            if result and result.strip():
                msg = f"cleanup output suspiciously short ({_normalized_len(result)} vs {_normalized_len(chunk_text)} normalized chars) — keeping raw text."
            else:
                msg = "cleanup failed after retries — keeping raw text."
            print(f"[{course_code}] {chunk_label}: {msg}")
            chunk_row.status = "COMPLETED"
            chunk_row.cleaned_text = chunk_text
            chunk_row.error_message = msg
            chunk_row.save(update_fields=["status", "cleaned_text", "error_message", "updated_at"])

        time.sleep(3)

    # Reassemble from all COMPLETED rows, in order — includes both real
    # cleanups and raw-text fallbacks saved above.
    final_rows = slide.cleanup_chunks.order_by("chunk_index")
    return "\n\n".join(row.cleaned_text for row in final_rows)


def _cleanup_mangled_text_no_resume(course_code, course_title, raw_text):
    """Original non-persistent behavior, kept for any caller that doesn't
    pass a slide object."""
    client = _get_batch_client()
    chunks = _macro_chunk_text(raw_text)
    cleaned_chunks = []

    for idx, chunk_text in enumerate(chunks):
        chunk_label = f"Cleanup chunk {idx + 1}/{len(chunks)}"
        result = _call_gemini_with_retry(
            client=client,
            model="gemini-3.6-flash",
            contents=(
                f"Course: {course_code} — {course_title}\n\n"
                f"Raw extracted slide text (chunk {idx + 1} of {len(chunks)}):\n{chunk_text}"
            ),
            config=types.GenerateContentConfig(
                system_instruction=CLEANUP_SYSTEM_INSTRUCTION,
                max_output_tokens=8000,
            ),
            course_code=course_code,
            chunk_label=chunk_label,
        )

        if result and result.strip() and _normalized_len(result) >= _normalized_len(chunk_text) * 0.6:
            cleaned_chunks.append(result.strip())
        else:
            if result and result.strip():
                print(f"[{course_code}] {chunk_label}: cleanup output suspiciously short — keeping raw text.")
            else:
                print(f"[{course_code}] {chunk_label}: cleanup failed, keeping raw text for this chunk.")
            cleaned_chunks.append(chunk_text)
        time.sleep(3)

    return "\n\n".join(cleaned_chunks)

TOPIC_SPLIT_SYSTEM_INSTRUCTION = (
    "You are splitting one week's worth of university lecture slide text "
    "into per-topic buckets, using a fixed list of topic names for that "
    "week.\n\n"
    "Rules:\n"
    "1. Assign every portion of the text to exactly ONE of the given "
    "topics — the one it most directly teaches. Do not duplicate content "
    "across topics.\n"
    "2. Preserve the original text verbatim inside each bucket — copy, "
    "never summarize, paraphrase, or shorten. Keep all formulas, numbers, "
    "worked examples, and tables exactly as they appear in the source.\n"
    "3. General introductory or transitional material that applies to the "
    "whole week (not clearly tied to one topic) should go into whichever "
    "topic it most directly sets up — usually the first topic it precedes. "
    "This is a NARROW exception for a sentence or two of framing text only "
    "— it is NEVER a reason to assign a large block of content, a full "
    "worked example, or tutorial questions to a topic just because you're "
    "uncertain. If content clearly belongs to a DIFFERENT named topic (a "
    "worked example on Vogel's equation, tutorial questions on decline "
    "curves, etc.), it must go to THAT topic even if it also appears near "
    "text for this one.\n"
    "4. If a topic has no corresponding content anywhere in this week's "
    "text, return an empty string for that topic's key. Do not invent "
    "content to fill it, and do not pad it with unrelated material.\n"
    "5. NEVER assign the same sentence or paragraph to more than one "
    "topic. Each portion of source text belongs to exactly one bucket.\n"
    "6. Be suspicious of any single topic accumulating a disproportionate "
    "share of the total text — that usually means unrelated content was "
    "misassigned to it. Re-check ambiguous content against ALL topic "
    "names before defaulting to one.\n"
    "7. Return ONLY a JSON object mapping each given topic name (exactly "
    "as provided) to its text. No markdown fences, no explanation, no "
    "keys other than the topic names given.\n\n"
    "Example output shape for topics [\"A\", \"B\"]:\n"
    '{"A": "...text for A...", "B": "...text for B..."}'
)


def split_week_chunk_by_topic(slide, week_number, topic_names):
    """Split one week's SlideChunk.chunk_text into per-topic buckets and
    save them as SlideTopicChunk rows. Idempotent. Returns (success, count).
    On any failure, returns (False, 0) and leaves prior rows untouched —
    callers fall back to the week-level SlideChunk, never lose content."""
    from .models import SlideChunk

    if not topic_names:
        return False, 0

    week_chunk = slide.chunks.filter(week_number=week_number).first()
    if not week_chunk or not week_chunk.chunk_text.strip():
        return False, 0

    batch_client = _get_batch_client()
    topics_list_str = "\n".join(f"- {t}" for t in topic_names)

    raw = _call_gemini_with_retry(
        client=batch_client,
        model="gemini-3.6-flash",
        contents=(
            f"Course: {slide.course_code} — {slide.course_title}\n"
            f"Week {week_number} topics (split into exactly these buckets):\n"
            f"{topics_list_str}\n\n"
            f"Week {week_number} slide text:\n{week_chunk.chunk_text}"
        ),
        config=types.GenerateContentConfig(
            system_instruction=TOPIC_SPLIT_SYSTEM_INSTRUCTION,
            max_output_tokens=8000,
        ),
        course_code=slide.course_code,
        chunk_label=f"Topic split — Week {week_number}",
    )

    if not raw or not raw.strip():
        print(f"[{slide.course_code}] Topic split week {week_number}: no response, keeping week-level chunk.")
        return False, 0

    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip()

    try:
        split_map = json.loads(clean)
        if not isinstance(split_map, dict):
            raise ValueError("Model returned non-dict JSON.")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[{slide.course_code}] Topic split week {week_number}: JSON parse failed ({e}).")
        return False, 0

    total_split_len = sum(len(str(v)) for v in split_map.values())
    source_len = len(week_chunk.chunk_text)
    if source_len > 0 and total_split_len < source_len * 0.6:
        print(f"[{slide.course_code}] Topic split week {week_number}: output too short vs source — discarding.")
        return False, 0

    created = 0
    for topic_name in topic_names:
        topic_text = str(split_map.get(topic_name, "")).strip()
        SlideTopicChunk.objects.update_or_create(
            slide=slide,
            week_number=week_number,
            topic_name=topic_name,
            defaults={"chunk_text": topic_text, "is_empty": not bool(topic_text)},
        )
        created += 1

    print(f"[{slide.course_code}] Topic split week {week_number}: {created} topic chunks saved.")
    return True, created


def split_all_weeks_by_topic(slide):
    """Always split slide content against the slide's own AI-extracted
    topics — CourseOutline no longer supplies week-grouped topic buckets."""
    split_slide_by_extracted_topics(slide)

def split_slide_by_extracted_topics(slide):
    if not slide.extracted_topics or not slide.extracted_text.strip():
        return False, 0

    batch_client = _get_batch_client()
    topics_list_str = "\n".join(f"- {t}" for t in slide.extracted_topics)

    # Smaller chunks — verbatim reproduction of dense LaTeX/formula text
    # inflates output size well beyond the source char count, so keep
    # input chunks conservative to leave headroom under max_output_tokens.
    source_chunks = _macro_chunk_text(slide.extracted_text, target_chunk_size=4000, min_chunks=1, max_chunks=None)
    if not source_chunks:
        return False, 0

    accumulated = {t: [] for t in slide.extracted_topics}
    any_success = False

    def _try_parse(raw):
        if not raw or not raw.strip():
            return None
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip()
        # Robustness: if there's leading/trailing junk around the JSON
        # object, slice to the outermost braces before parsing.
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1 and end > start:
            clean = clean[start:end + 1]
        try:
            parsed = json.loads(clean)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None

    for idx, source_chunk in enumerate(source_chunks):
        split_map = None
        for attempt in range(3):  # retry on parse failure, not just API error
            raw = _call_gemini_with_retry(
                client=batch_client,
                model="gemini-3.6-flash",
                contents=(
                    f"Course: {slide.course_code} — {slide.course_title}\n"
                    f"Full topic list for this course (only some may appear in "
                    f"this excerpt — return an empty string for any topic not "
                    f"present here):\n{topics_list_str}\n\n"
                    f"Slide text excerpt {idx + 1} of {len(source_chunks)}:\n{source_chunk}"
                ),
                config=types.GenerateContentConfig(
                    system_instruction=TOPIC_SPLIT_SYSTEM_INSTRUCTION,
                    max_output_tokens=8000,
                ),
                course_code=slide.course_code,
                chunk_label=f"Topic split — excerpt {idx + 1}/{len(source_chunks)} (attempt {attempt + 1})",
            )
            split_map = _try_parse(raw)
            if split_map is not None:
                break
            print(f"[{slide.course_code}] Excerpt {idx + 1} attempt {attempt + 1}: parse failed, retrying...")
            time.sleep(3)

        if split_map is None:
            print(f"[{slide.course_code}] Excerpt {idx + 1}: all attempts failed — content for this excerpt lost.")
            time.sleep(3)
            continue

        for topic_name in slide.extracted_topics:
            piece = str(split_map.get(topic_name, "")).strip()
            if piece:
                accumulated[topic_name].append(piece)

        any_success = True
        time.sleep(3)

    if not any_success:
        print(f"[{slide.course_code}] Topic split (no outline): all excerpts failed.")
        return False, 0

    created = 0
    for topic_name in slide.extracted_topics:
        topic_text = "\n\n".join(accumulated[topic_name]).strip()
        SlideTopicChunk.objects.update_or_create(
            slide=slide, week_number=0, topic_name=topic_name,
            defaults={"chunk_text": topic_text, "is_empty": not bool(topic_text)},
        )
        created += 1

    print(f"[{slide.course_code}] Topic split (no outline): {created} topic chunks saved.")
    return True, created

def retry_missing_topic_splits(slide, topic_names=None):
    """Re-run the split ONLY for topics currently empty (or explicitly
    named), without touching already-populated topic chunks. Safer than
    re-running split_slide_by_extracted_topics wholesale, since that
    resends every excerpt and can overwrite good results with a fresh,
    non-deterministic (and possibly worse) response."""
    if topic_names is None:
        topic_names = list(
            slide.topic_chunks.filter(is_empty=True).values_list("topic_name", flat=True)
        )
    if not topic_names:
        print(f"[{slide.course_code}] No empty topics to retry.")
        return True, 0

    batch_client = _get_batch_client()
    topics_list_str = "\n".join(f"- {t}" for t in topic_names)
    source_chunks = _macro_chunk_text(slide.extracted_text, target_chunk_size=4000, min_chunks=1, max_chunks=None)

    accumulated = {t: [] for t in topic_names}

    def _try_parse(raw):
        if not raw or not raw.strip():
            return None
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip()
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1 and end > start:
            clean = clean[start:end + 1]
        try:
            parsed = json.loads(clean)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None

    for idx, source_chunk in enumerate(source_chunks):
        split_map = None
        for attempt in range(3):
            raw = _call_gemini_with_retry(
                client=batch_client,
                model="gemini-3.6-flash",
                contents=(
                    f"Course: {slide.course_code} — {slide.course_title}\n"
                    f"ONLY look for these specific topics (others may exist "
                    f"in the course but are already handled — ignore them):\n"
                    f"{topics_list_str}\n\n"
                    f"Slide text excerpt {idx + 1} of {len(source_chunks)}:\n{source_chunk}"
                ),
                config=types.GenerateContentConfig(
                    system_instruction=TOPIC_SPLIT_SYSTEM_INSTRUCTION,
                    max_output_tokens=8000,
                ),
                course_code=slide.course_code,
                chunk_label=f"Retry missing — excerpt {idx + 1}/{len(source_chunks)} (attempt {attempt + 1})",
            )
            split_map = _try_parse(raw)
            if split_map is not None:
                break
            time.sleep(3)

        if split_map:
            for topic_name in topic_names:
                piece = str(split_map.get(topic_name, "")).strip()
                if piece:
                    accumulated[topic_name].append(piece)
        time.sleep(3)

    filled = 0
    for topic_name in topic_names:
        topic_text = "\n\n".join(accumulated[topic_name]).strip()
        if topic_text:
            SlideTopicChunk.objects.update_or_create(
                slide=slide, week_number=0, topic_name=topic_name,
                defaults={"chunk_text": topic_text, "is_empty": False},
            )
            filled += 1

    print(f"[{slide.course_code}] Retry missing: {filled}/{len(topic_names)} topics filled.")
    return True, filled