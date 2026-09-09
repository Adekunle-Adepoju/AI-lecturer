import json
import time
from google import genai
from google.genai import types
from django.conf import settings
import math
from .models import SlideDocument, SlideExtractionChunk


def _get_batch_client():
    return genai.Client(api_key=settings.GEMINI_API_KEY_EXTRACTION)


def _call_gemini_with_retry(client, model, contents, config, course_code, chunk_label, max_retries=3):
    """Calls Gemini with 429-aware retry. Returns response.text on success,
    or None if the chunk fails after retries (never raises — caller decides what to do)."""
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            return response.text
        except Exception as e:
            error_str = str(e)
            is_rate_limit = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str

            if is_rate_limit:
                print(f"[{course_code}] {chunk_label}: 429 rate limit hit (attempt {attempt + 1}/{max_retries}) — sleeping 20s and retrying...")
                time.sleep(20)
                continue  # retry the SAME chunk, never skip on a rate limit
            else:
                print(f"[{course_code}] {chunk_label}: non-rate-limit error ({e}) — attempt {attempt + 1}/{max_retries}")
                if attempt < max_retries - 1:
                    time.sleep(3)
                    continue
                # Final attempt failed for a non-rate-limit reason
                return None

    # Exhausted all retries (including repeated 429s)
    print(f"[{course_code}] {chunk_label}: exhausted {max_retries} retries — giving up on this chunk.")
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
    """Extract genuine gridded tables (flow-regime comparisons, property
    tables, etc.) as clean Markdown tables instead of letting them get
    smashed into the linear text stream. Returns '' if no tables found."""
    try:
        tables = page.extract_tables()
    except Exception:
        return ""

    if not tables:
        return ""

    blocks = []
    for table in tables:
        if not table or len(table) < 2:
            continue  # skip 0/1-row "tables" — usually extraction noise, not real tables

        rows = [[(cell or "").strip().replace("\n", " ") for cell in row] for row in table]
        header = rows[0]
        body = rows[1:]

        if not any(h.strip() for h in header):
            continue  # blank header row — likely a false-positive table detection

        md_lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(["---"] * len(header)) + " |",
        ]
        for row in body:
            # Pad/truncate row to header length so malformed rows don't break the table
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

    extracted_text = _cleanup_mangled_text_with_ai(slide.course_code, slide.course_title, extracted_text)

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
    else:
        slide.extracted_topics = []
        slide.topics_incomplete = False
        slide.save(update_fields=["extracted_topics", "topics_incomplete"])

def _macro_chunk_text(text, target_chunk_size=17500, min_chunks=3, max_chunks=5):
    """Split text into a small number of large chunks (3-5), each roughly
    15,000-20,000 chars, instead of many small fixed-size chunks. Reduces
    total API calls (fewer 429s) while staying well under output-truncation
    risk from sending the whole document in one call."""
    total_len = len(text)

    if total_len <= target_chunk_size:
        return [text] if text.strip() else []

    # Start from the target size, then clamp the resulting chunk COUNT to [min_chunks, max_chunks]
    num_chunks = math.ceil(total_len / target_chunk_size)
    num_chunks = max(min_chunks, min(num_chunks, max_chunks))

    # Recompute actual chunk size so `num_chunks` divides the text evenly
    actual_chunk_size = math.ceil(total_len / num_chunks)

    chunks = [
        text[i:i + actual_chunk_size]
        for i in range(0, total_len, actual_chunk_size)
    ]
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


def _cleanup_mangled_text_with_ai(course_code, course_title, raw_text):
    """Runs the raw extracted text through Gemini in macro-chunks to repair
    column-smashing and ASCII-diagram mangling that layout-aware pdfplumber
    extraction alone doesn't fully fix. Falls back to the raw text for any
    chunk that fails, so a cleanup failure never loses content — it just
    leaves that portion un-prettified rather than blank."""
    if not raw_text.strip():
        return raw_text

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

        if result and result.strip() and len(result.strip()) >= len(chunk_text) * 0.6:
            cleaned_chunks.append(result.strip())
        else:
            # Missing, empty, or suspiciously short relative to input —
            # likely truncated or a degenerate response. Falling back to
            # raw text avoids silently losing slide content.
            if result and result.strip():
                print(f"[{course_code}] {chunk_label}: cleanup output suspiciously short ({len(result.strip())} vs {len(chunk_text)} chars) — keeping raw text.")
            else:
                print(f"[{course_code}] {chunk_label}: cleanup failed, keeping raw text for this chunk.")
            cleaned_chunks.append(chunk_text)
        time.sleep(3)

    return "\n\n".join(cleaned_chunks)