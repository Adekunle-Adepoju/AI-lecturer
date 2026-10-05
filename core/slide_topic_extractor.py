import json
import time
import re
from google import genai
from google.genai import types
from django.conf import settings
import math
import os

from .models import SlideDocument, SlideExtractionChunk, SlideTopicChunk, CourseOutline
from .models import SlideTopicSplitChunk
from dataclasses import dataclass

# ─── Page-based splitting helpers ──────────────────────────────────────────────
PAGE_MARKER_RE = re.compile(r"^--- Page (\d+) ---[ \t]*$", re.MULTILINE)
CONTINUATION_RE = re.compile(r"^--- \[Slide Continuation\] ---[ \t]*$", re.MULTILINE)
LLM_PAGE_RE = re.compile(r"^\[\[PAGE (\d+)\]\]$", re.MULTILINE)

TOPIC_PAGE_MAP_INSTRUCTION = (
    "You are mapping university lecture slide pages to topics. You will be given a "
    "list of topic names and an excerpt of slide text divided into pages, each "
    "starting with a line like [[PAGE 12]].\n\n"
    "For each topic, list the numbers of the pages in THIS excerpt that teach it.\n"
    "Rules:\n"
    "1. Return page numbers ONLY. Never return any slide text.\n"
    "2. Use only page numbers that appear in [[PAGE n]] markers in this excerpt.\n"
    "3. A page may be listed under more than one topic if it genuinely teaches both. "
    "But if one topic's name is a broad umbrella over other, more specific topics in "
    "the list, list a page under the specific topic only, not under the umbrella.\n"
    "4. A pure title page, agenda, or 'thank you' page belongs to no topic.\n"
    "5. If none of this excerpt's pages teach a topic, return an empty list for it.\n"
    "6. Use topic names exactly as given. Return ONLY a JSON object, no markdown "
    'fences, e.g. {"Topic A": [3, 4], "Topic B": []}'
)


@dataclass
class Page:
    seq: int      # 1-based position in the document; unique even if page numbers repeat
    label: str    # "Page 30", or "Section 4" when the text has no page markers
    text: str

    def render(self):
        return f"--- {self.label} ---\n\n{self.text}"


def _fallback_sections(text, target_chars=1500):
    """No page markers in the text: group whole paragraphs into pseudo-pages."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    sections, current = [], ""
    for para in paragraphs:
        if current and len(current) + len(para) > target_chars:
            sections.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        sections.append(current)
    return [Page(i, f"Section {i}", s) for i, s in enumerate(sections, start=1)]


def split_pages(text):
    text = CONTINUATION_RE.sub("", text or "")
    matches = list(PAGE_MARKER_RE.finditer(text))
    if not matches:
        return _fallback_sections(text)
    raw = []
    preamble = text[:matches[0].start()].strip()
    if preamble:
        raw.append(("Front matter", preamble))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        if body:
            raw.append((f"Page {m.group(1)}", body))
    return [Page(i, label, body) for i, (label, body) in enumerate(raw, start=1)]


def group_pages_into_excerpts(pages, target_chars=9000):
    """Group WHOLE pages into excerpts. Never cuts inside a page."""
    excerpts, current, size = [], [], 0
    for p in pages:
        plen = len(p.text) + 40
        if current and size + plen > target_chars:
            excerpts.append(current)
            current, size = [], 0
        current.append(p)
        size += plen
    if current:
        excerpts.append(current)
    return excerpts


def render_for_llm(pages):
    return "\n\n".join(f"[[PAGE {p.seq}]]\n{p.text}" for p in pages)


def rendered_page_numbers(rendered):
    return {int(n) for n in LLM_PAGE_RE.findall(rendered or "")}


def build_chunk_text(pages_by_seq, seqs):
    """Build a topic chunk by copying pages straight from the source text."""
    return "\n\n".join(pages_by_seq[s].render() for s in sorted(set(seqs)) if s in pages_by_seq)


def _norm(s):
    return re.sub(r"\s+", " ", str(s)).strip().casefold()


def parse_page_map(raw, topic_names, valid_seqs):
    """Returns {topic: [page seqs]} or None if the response is unusable.
    Unknown topics and page numbers not in this excerpt are silently dropped."""
    if not raw or not raw.strip():
        return None
    clean = raw.replace("```json", "").replace("```", "").strip()
    a, b = clean.find("{"), clean.rfind("}")
    if a == -1 or b <= a:
        return None
    try:
        data = json.loads(clean[a:b + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    canonical = {_norm(t): t for t in topic_names}
    result = {t: [] for t in topic_names}
    for key, value in data.items():
        topic = canonical.get(_norm(key))
        if topic is None or not isinstance(value, list):
            continue
        seqs = set()
        for v in value:
            if isinstance(v, bool):
                continue
            if isinstance(v, str) and v.strip().isdigit():
                v = int(v.strip())
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            if isinstance(v, int) and v in valid_seqs:
                seqs.add(v)
        result[topic] = sorted(set(result[topic]) | seqs)
    return result


def _markers_preserved(original, cleaned):
    return PAGE_MARKER_RE.findall(original) == PAGE_MARKER_RE.findall(cleaned)


def _get_batch_client():
    return genai.Client(
        api_key=settings.GEMINI_API_KEY_EXTRACTION,
        http_options=types.HttpOptions(timeout=60_000),  # ms — raises instead of hanging forever
    )

def _get_cleanup_client():
    return genai.Client(
        api_key=settings.GEMINI_API_KEY_CLEANUP,
        http_options=types.HttpOptions(timeout=60_000),
    )

DEFAULT_MODEL_FALLBACK_CHAIN = ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"]


DIAGRAM_REPEAT_LIMIT = 4   # a picture shown on this many pages is template decoration
DIAGRAM_FAIL_LIMIT = 3     # stop asking Gemini after this many pages fail in a row


def _call_gemini_with_retry(client, model, contents, config, course_code, chunk_label,
                            max_retries=3, model_fallback_chain=None, wait_seconds=20):
    chain = model_fallback_chain or DEFAULT_MODEL_FALLBACK_CHAIN

    if model in chain:
        start_idx = chain.index(model)
        models_to_try = chain[start_idx:] + chain[:start_idx]
    else:
        models_to_try = [model] + [m for m in chain if m != model]

    for model_idx, current_model in enumerate(models_to_try):
        is_last_model = (model_idx == len(models_to_try) - 1)

        for attempt in range(max_retries):
            is_last_attempt = (attempt == max_retries - 1)
            try:
                response = client.models.generate_content(
                    model=current_model, contents=contents, config=config,
                )
                if model_idx > 0:
                    print(f"[{course_code}] {chunk_label}: succeeded on fallback model {current_model}.")
                return response.text
            except Exception as e:
                error_str = str(e)
                is_rate_limit = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
                is_unavailable = "503" in error_str or "UNAVAILABLE" in error_str

                if is_rate_limit or is_unavailable:
                    if is_last_attempt:
                        # Waiting now would be pointless: we are about to give up on
                        # this model (and try the next one straight away).
                        print(f"[{course_code}] {chunk_label}: {current_model} — 429/503 (attempt {attempt + 1}/{max_retries}) — giving up on this model.")
                        break
                    print(f"[{course_code}] {chunk_label}: {current_model} — 429/503 (attempt {attempt + 1}/{max_retries}) — sleeping {wait_seconds}s...")
                    time.sleep(wait_seconds)
                    continue
                else:
                    print(f"[{course_code}] {chunk_label}: {current_model} — non-rate-limit error ({e}) — attempt {attempt + 1}/{max_retries}")
                    if not is_last_attempt:
                        time.sleep(3)
                        continue
                    break

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
                max_output_tokens=8000,
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

    merged = dedup_topics_semantically(slide)
    

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

def _diagram_candidate_pages(fdoc):
    """1-based numbers of pages that carry a REAL picture. A picture that
    repeats on DIAGRAM_REPEAT_LIMIT or more pages is the slide template's
    background/logo, so it is ignored (it used to cost one Gemini call per page)."""
    per_page, seen = [], {}
    for page in fdoc:
        try:
            infos = page.get_image_info(hashes=True)
        except Exception:
            infos = []
        per_page.append((page.rect.width * page.rect.height, infos))
        for d in {im.get("digest") for im in infos if im.get("digest")}:
            seen[d] = seen.get(d, 0) + 1

    result = []
    for number, (area, infos) in enumerate(per_page, start=1):
        for im in infos:
            d = im.get("digest")
            if d and seen.get(d, 0) >= DIAGRAM_REPEAT_LIMIT:
                continue
            b = im["bbox"]
            share = ((b[2] - b[0]) * (b[3] - b[1])) / area if area else 0
            if 0.08 < share < 0.85:
                result.append(number)
                break
    return result


def _describe_page_diagram(fitz_page, client, course_code, page_number):
    """Returns (line, failed).
    line   = '[DIAGRAM: ...]', or '' when the picture is only decoration.
    failed = True only when Gemini could not be reached on ANY model."""
    pix = fitz_page.get_pixmap(dpi=90)
    img_bytes = pix.tobytes("jpeg")
    pix = None  # free the pixel buffer right away

    prompt = (
        "This is one lecture slide. If it contains a diagram, figure, photo or chart, "
        "reply with ONE line that starts with [DIAGRAM: and ends with ]. Inside, list only "
        "the labels visible in it and how they are connected or positioned (for example: "
        "'X sits above Y; an arrow runs from A to B'). For a chart, list axis labels and "
        "any values you can read. Do not name anything that is not labelled and do not add "
        "facts. If the picture is only a logo or decoration, reply NONE."
    )
    reply = _call_gemini_with_retry(
        client=client,
        model="gemini-3.6-flash",
        contents=[types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"), prompt],
        config=types.GenerateContentConfig(max_output_tokens=2000),
        course_code=course_code,
        chunk_label=f"Diagram page {page_number}",
        max_retries=2,   # and now the full model fallback chain, like every other step
    )
    if reply is None:
        return "", True
    reply = " ".join(reply.split())
    a, b = reply.find("[DIAGRAM:"), reply.rfind("]")
    if a != -1 and b > a:
        return reply[a:b + 1], False
    return "", False


def _page_numbers(text):
    return [int(n) for n in PAGE_MARKER_RE.findall(text or "")]


def _pages_with_diagram_line(text):
    text = text or ""
    markers = list(PAGE_MARKER_RE.finditer(text))
    have = set()
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        if "[DIAGRAM:" in text[m.end():end]:
            have.add(int(m.group(1)))
    return have


def _insert_diagram_line(text, page_number, line):
    """Put `line` at the end of that page's block (or add the block in page order)."""
    text = text or ""
    markers = list(PAGE_MARKER_RE.finditer(text))
    for i, m in enumerate(markers):
        if int(m.group(1)) == page_number:
            end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
            block = text[m.end():end]
            if "[DIAGRAM:" in block:
                return text
            return text[:m.end()] + block.rstrip() + f"\n\n{line}\n\n" + text[end:]
    # The page had no text at all, so it never got a marker: add a new block in order.
    new_block = f"--- Page {page_number} ---\n\n{line}\n\n"
    for m in markers:
        if int(m.group(1)) > page_number:
            return text[:m.start()] + new_block + text[m.start():]
    return (text.rstrip() + "\n\n" + new_block) if text.strip() else new_block


def _patch_slide_with_diagram(slide, page_number, line):
    """Add the description to slide.extracted_text AND to the saved cleanup rows.
    (If only extracted_text were changed, 'Retry cleanup' would rebuild the text
    from the cleanup rows and silently throw the new description away.)"""
    slide.extracted_text = _insert_diagram_line(slide.extracted_text, page_number, line)
    slide.save(update_fields=["extracted_text"])

    rows = list(slide.cleanup_chunks.order_by("chunk_index"))
    if not rows:
        return
    target = next((r for r in rows if page_number in _page_numbers(r.chunk_text)), None)
    if target is None:
        target = next((r for r in rows if any(n > page_number for n in _page_numbers(r.chunk_text))), rows[-1])
    target.chunk_text = _insert_diagram_line(target.chunk_text, page_number, line)
    target.cleaned_text = _insert_diagram_line(target.cleaned_text, page_number, line)
    target.save(update_fields=["chunk_text", "cleaned_text", "updated_at"])


def retry_missing_diagrams(slide):
    """Ask Gemini again ONLY for pages that carry a real picture but still have no
    [DIAGRAM: ...] line. Returns (added, still_missing)."""
    import fitz

    if (slide.append_count or 1) > 1:
        raise RuntimeError("This deck was built from several uploads, so page numbers repeat. "
                           "Re-upload it as one PDF to redo its diagrams.")
    if not slide.file or not os.path.isfile(slide.file.path):
        raise RuntimeError("The original PDF is no longer on disk (Render clears files on restart). Re-upload the slide.")
    if not (slide.extracted_text or "").strip():
        raise RuntimeError("No extracted text saved for this slide.")

    fdoc = fitz.open(slide.file.path)
    try:
        have = _pages_with_diagram_line(slide.extracted_text)
        todo = [n for n in _diagram_candidate_pages(fdoc) if n not in have]
        if not todo:
            print(f"[{slide.course_code}] Diagrams: every picture page already has a description.")
            return 0, []

        client = _get_batch_client()
        added, still_missing, in_a_row = 0, [], 0
        for pos, n in enumerate(todo):
            line, failed = _describe_page_diagram(fdoc[n - 1], client, slide.course_code, n)
            if failed:
                still_missing.append(n)
                in_a_row += 1
                if in_a_row >= DIAGRAM_FAIL_LIMIT:
                    still_missing.extend(todo[pos + 1:])
                    print(f"[{slide.course_code}] Gemini keeps failing — stopping diagram retry. Try again later.")
                    break
                continue
            in_a_row = 0
            if line:
                _patch_slide_with_diagram(slide, n, line)
                added += 1
            time.sleep(1)
        print(f"[{slide.course_code}] Diagram retry: {added} added, {len(still_missing)} still missing.")
        return added, still_missing
    finally:
        fdoc.close()

def _parse_slide_document(slide):
    import gc
    import pdfplumber
    import fitz

    extracted_text = ""
    fdoc = None
    diagram_pages, diagram_failed = [], []
    try:
        fdoc = fitz.open(slide.file.path)
        vision_client = _get_batch_client()
        candidates = set(_diagram_candidate_pages(fdoc))
        diagram_pages = sorted(candidates)
        vision_on, in_a_row = True, 0
        with pdfplumber.open(slide.file.path) as pdf:
            total = len(pdf.pages)
            print(f"[{slide.course_code}] {len(candidates)} of {total} pages carry a real picture "
                  f"(pictures repeated on every page are ignored).", flush=True)
            for page_number, page in enumerate(pdf.pages, start=1):
                print(f"[{slide.course_code}] Extracting page {page_number}/{total}", flush=True)
                text = (_extract_page_text_layout_aware(page) or "").strip()
                page.flush_cache()  # release pdfplumber's per-page cache

                if page_number in candidates:
                    if vision_on:
                        diagram, failed = _describe_page_diagram(
                            fdoc[page_number - 1], vision_client, slide.course_code, page_number
                        )
                    else:
                        diagram, failed = "", True
                    if failed:
                        diagram_failed.append(page_number)
                        in_a_row += 1
                        if vision_on and in_a_row >= DIAGRAM_FAIL_LIMIT:
                            vision_on = False
                            print(f"[{slide.course_code}] Gemini keeps failing — skipping diagram "
                                  f"descriptions for the rest of this deck. Use 'Retry diagrams' later.", flush=True)
                    else:
                        in_a_row = 0
                    if diagram:
                        text = f"{text}\n\n{diagram}".strip()

                if text:
                    extracted_text += f"--- Page {page_number} ---\n\n{text}\n\n"
                if page_number % 10 == 0:
                    gc.collect()
    except Exception as e:
        print(f"PDF extraction failed for {slide.course_code}: {e}")
        raise
    finally:
        if fdoc:
            fdoc.close()

    if not extracted_text.strip():
        raise RuntimeError(f"{slide.course_code}: no text could be extracted from the PDF.")

    # Save the raw text right away, so a crash in the long AI steps below never loses it.
    slide.extracted_text = extracted_text
    slide.save(update_fields=["extracted_text"])

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

    return {"diagram_pages": diagram_pages, "diagram_failed": diagram_failed}

def _macro_chunk_text(text, target_chunk_size=17500, min_chunks=3, max_chunks=None):
    """Split text into chunks near target_chunk_size, cutting ONLY at page
    markers (or blank lines / newlines / whitespace if there are none) —
    never mid-word. ''.join(chunks) always equals the input text."""
    total_len = len(text)
    if total_len <= target_chunk_size:
        return [text] if text.strip() else []

    if min_chunks and min_chunks > 1:
        target_chunk_size = min(target_chunk_size, math.ceil(total_len / min_chunks))

    cuts = [m.start() for m in PAGE_MARKER_RE.finditer(text)]
    if len(cuts) < 2:
        cuts = [m.end() for m in re.finditer(r"\n\s*\n", text)]
    if len(cuts) < 2:
        cuts = [m.end() for m in re.finditer(r"\n", text)]
    if len(cuts) < 2:
        cuts = [m.end() for m in re.finditer(r"\s", text)]

    bounds = sorted(set([0] + cuts + [total_len]))
    segments = [text[a:b] for a, b in zip(bounds, bounds[1:]) if b > a]

    chunks, current = [], ""
    for seg in segments:
        if current and len(current) + len(seg) > target_chunk_size:
            chunks.append(current)
            current = seg
        else:
            current += seg
    if current:
        chunks.append(current)
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
    "If the text has no structural problems at all, simply return it unchanged.\n"
    "Lines that start with '[DIAGRAM:' must be copied through exactly as they are. "
    "Never rewrite, merge or remove them.\n"
    "6. Lines like '--- Page 12 ---' are page markers. Copy every one exactly, on "
    "its own line, in the same order. Never remove, renumber, merge, or move them.\n\n"
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

    client = _get_cleanup_client()
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
                max_output_tokens=24000,
            ),
            course_code=course_code,
            chunk_label=chunk_label,
        )

        if (result and result.strip()
                and _normalized_len(result) >= _normalized_len(chunk_text) * 0.6
                and _markers_preserved(chunk_text, result)):
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
    client = _get_cleanup_client()
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

        if (result and result.strip()
                and _normalized_len(result) >= _normalized_len(chunk_text) * 0.6
                and _markers_preserved(chunk_text, result)):
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

def _ask_page_map(client, slide, pages, topic_names, label):
    """Ask Gemini which pages of this excerpt belong to which topics.
    Returns {topic: [seqs]} or None after 3 failed attempts."""
    rendered = render_for_llm(pages)
    valid = {p.seq for p in pages}
    topics_list_str = "\n".join(f"- {t}" for t in topic_names)

    for attempt in range(3):
        raw = _call_gemini_with_retry(
            client=client,
            model="gemini-3.6-flash",
            contents=(
                f"Course: {slide.course_code} — {slide.course_title}\n\n"
                f"Topics for this week:\n{topics_list_str}\n\n"
                f"Slide excerpt:\n{rendered}"
            ),
            config=types.GenerateContentConfig(
                system_instruction=TOPIC_PAGE_MAP_INSTRUCTION,
                max_output_tokens=8000,
            ),
            course_code=slide.course_code,
            chunk_label=label,
        )
        page_map = parse_page_map(raw, topic_names, valid)
        if page_map is not None:
            return page_map
        time.sleep(3)
    return None 


def _get_or_create_topic_split_chunks(slide, pages):
    """Excerpt rows now hold whole pages ([[PAGE n]] blocks). If existing rows
    don't match the slide's current text (old mid-word rows, or text appended
    since), they're deleted and rebuilt."""
    existing = list(slide.topic_split_chunks.order_by("chunk_index"))
    if existing:
        stored = "\n\n".join(r.chunk_text for r in existing)
        if stored == render_for_llm(pages):
            return existing
        print(f"[{slide.course_code}] Topic-split rows are stale or old-format — rebuilding.")
        slide.topic_split_chunks.all().delete()

    created = []
    for idx, excerpt in enumerate(group_pages_into_excerpts(pages)):
        created.append(SlideTopicSplitChunk.objects.create(
            slide=slide, chunk_index=idx,
            chunk_text=render_for_llm(excerpt), status="PENDING",
        ))
    return created

ORPHAN_PAGE_INSTRUCTION = (
    "You are placing university lecture slide pages that a first pass left without a topic. "
    "You will get a list of topic names and some pages, each starting with a line like [[PAGE 12]].\n\n"
    "For each page, choose the ONE topic from the list that the page most directly teaches, "
    "even when the match is only approximate.\n"
    "Answer NONE only if the page is a title slide, a course outline or agenda, a section "
    "divider, an 'end' or 'thank you' page, or has no teachable content.\n"
    "Rules: return page numbers and topic names only, never slide text. Use topic names exactly "
    "as given. Return ONLY a JSON object mapping the page number (as a string) to a topic name "
    'or "NONE", with no markdown fences, e.g. {"6": "Topic A", "9": "NONE"}'
)


def _is_content_page(page, min_chars=80):
    """Title-only pages ('End', 'Day 2', a bare divider) have nothing to teach."""
    return len(re.sub(r"\s+", " ", page.text).strip()) >= min_chars


def _parse_json_object(raw):
    if not raw or not raw.strip():
        return None
    clean = raw.replace("```json", "").replace("```", "").strip()
    a, b = clean.find("{"), clean.rfind("}")
    if a == -1 or b <= a:
        return None
    try:
        data = json.loads(clean[a:b + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _assign_orphan_pages(client, slide, orphans, topic_names, batch_size=12):
    """One extra, forced-choice pass for pages the first pass left out.
    Returns {page seq: topic}. Never invents topics, so lesson weeks do not shift."""
    canonical = {_norm(t): t for t in topic_names}
    valid = {p.seq for p in orphans}
    topics_list_str = "\n".join(f"- {t}" for t in topic_names)
    placed = {}

    for i in range(0, len(orphans), batch_size):
        batch = orphans[i:i + batch_size]
        body = "\n\n".join(f"[[PAGE {p.seq}]]\n{p.text[:1500]}" for p in batch)
        raw = _call_gemini_with_retry(
            client=client,
            model="gemini-3.6-flash",
            contents=(
                f"Course: {slide.course_code} — {slide.course_title}\n\n"
                f"Topics:\n{topics_list_str}\n\nPages to place:\n{body}"
            ),
            config=types.GenerateContentConfig(
                system_instruction=ORPHAN_PAGE_INSTRUCTION, max_output_tokens=2000,
            ),
            course_code=slide.course_code,
            chunk_label=f"Unplaced pages {i // batch_size + 1}",
        )
        data = _parse_json_object(raw)
        if not data:
            continue
        for key, value in data.items():
            m = re.search(r"\d+", str(key))
            seq = int(m.group()) if m else None
            topic = canonical.get(_norm(value)) if isinstance(value, str) else None
            if seq in valid and topic:
                placed[seq] = topic
        time.sleep(3)
    return placed

def split_slide_by_extracted_topics(slide):
    """Resumable. Gemini only returns PAGE NUMBERS per topic; the chunk text is
    copied from the untouched extracted_text by our own code, so it cannot be
    truncated, reworded, or mislabeled by the model."""
    if not slide.extracted_topics or not slide.extracted_text.strip():
        return False, 0

    pages = split_pages(slide.extracted_text)
    if not pages:
        return False, 0
    pages_by_seq = {p.seq: p for p in pages}
    topics = list(slide.extracted_topics)

    batch_client = _get_batch_client()
    chunk_rows = _get_or_create_topic_split_chunks(slide, pages)
    pending_rows = [
        c for c in chunk_rows
        if c.status != "COMPLETED" or not set(topics) <= set((c.split_result or {}).keys())
    ]

    if not pending_rows:
        print(f"[{slide.course_code}] All {len(chunk_rows)} topic-split chunks already completed.")

    for chunk_row in pending_rows:
        label = f"Topic split — chunk {chunk_row.chunk_index + 1}/{len(chunk_rows)}"
        excerpt_pages = [pages_by_seq[s] for s in sorted(rendered_page_numbers(chunk_row.chunk_text))
                         if s in pages_by_seq]

        page_map = _ask_page_map(batch_client, slide, excerpt_pages, topics, label)

        if page_map is None:
            chunk_row.status = "FAILED"
            chunk_row.error_message = "All attempts failed (rate limit, network, or bad JSON)."
            chunk_row.save(update_fields=["status", "error_message", "updated_at"])
        else:
            chunk_row.status = "COMPLETED"
            chunk_row.split_result = page_map
            chunk_row.error_message = ""
            chunk_row.save(update_fields=["status", "split_result", "error_message", "updated_at"])
        time.sleep(3)

    # Aggregate page numbers from every COMPLETED excerpt, then build the
    # chunk text directly from the source pages.
    topic_seqs = {t: set() for t in topics}
    assigned = set()
    for row in slide.topic_split_chunks.filter(status="COMPLETED"):
        result = row.split_result or {}
        for t in topics:
            for s in result.get(t, []):
                if s in pages_by_seq:
                    topic_seqs[t].add(s)
                    assigned.add(s)

    # Pages the first pass left without a topic: one extra forced-choice pass
    orphans = [p for p in pages if p.seq not in assigned and _is_content_page(p)]
    if orphans:
        placed = _assign_orphan_pages(batch_client, slide, orphans, topics)
        for seq, topic in placed.items():
            topic_seqs[topic].add(seq)
            assigned.add(seq)
        print(f"[{slide.course_code}] Unplaced pages: {len(placed)} of {len(orphans)} placed in a topic.")
    created = 0
    for t in topics:
        text = build_chunk_text(pages_by_seq, topic_seqs[t])
        SlideTopicChunk.objects.update_or_create(
            slide=slide, week_number=0, topic_name=t,
            defaults={"chunk_text": text, "is_empty": not bool(text)},
        )
        created += 1

    unassigned = [pages_by_seq[s].label for s in sorted(set(pages_by_seq) - assigned)]
    any_failed = slide.topic_split_chunks.filter(status="FAILED").exists()
    print(f"[{slide.course_code}] Topic split: {created} topic chunks saved"
          f"{' (some excerpts FAILED — retry to fill gaps)' if any_failed else ''}.")
    if unassigned:
        print(f"[{slide.course_code}] {len(unassigned)} page(s) assigned to NO topic: "
              f"{', '.join(unassigned[:30])}{'...' if len(unassigned) > 30 else ''}")
    return True, created

def split_all_weeks_by_topic(slide):
    """Kept because _parse_slide_document and older code still call this name."""
    return split_slide_by_extracted_topics(slide)

def retry_missing_topic_splits(slide, topic_names=None):
    """Re-run the page mapping ONLY for topics currently empty (or named),
    leaving already-populated topic chunks untouched."""
    if topic_names is None:
        topic_names = list(
            slide.topic_chunks.filter(is_empty=True).values_list("topic_name", flat=True)
        )
    if not topic_names:
        print(f"[{slide.course_code}] No empty topics to retry.")
        return True, 0

    pages = split_pages(slide.extracted_text)
    pages_by_seq = {p.seq: p for p in pages}
    excerpts = group_pages_into_excerpts(pages)
    batch_client = _get_batch_client()

    accumulated = {t: set() for t in topic_names}
    for idx, excerpt in enumerate(excerpts):
        page_map = _ask_page_map(
            batch_client, slide, excerpt, topic_names,
            f"Retry missing — excerpt {idx + 1}/{len(excerpts)}",
        )
        if page_map:
            for t in topic_names:
                accumulated[t].update(page_map.get(t, []))
        time.sleep(3)

    filled = 0
    for t in topic_names:
        if accumulated[t]:
            SlideTopicChunk.objects.update_or_create(
                slide=slide, week_number=0, topic_name=t,
                defaults={"chunk_text": build_chunk_text(pages_by_seq, accumulated[t]),
                          "is_empty": False},
            )
            filled += 1

    print(f"[{slide.course_code}] Retry missing: {filled}/{len(topic_names)} topics filled.")
    return True, filled

def is_reference_table_content(text, max_len=1200):
    """Heuristic: is this chunk mostly a bare table/list of labels+numbers
    with little to no explanatory prose? If so, it should be rendered
    directly rather than expanded into a full lecture."""
    if len(text) > max_len:
        return False

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return False

    # Lines that look like "Label ... number+unit" (ft, m, psi, md, etc.)
    number_unit_line = re.compile(
        r'\d+[\s,]*\d*\s*(ft|feet|m|meters|psi|md|rb|scf|stb|max|maximum)\b',
        re.IGNORECASE
    )
    numeric_lines = sum(1 for l in lines if number_unit_line.search(l))

    # High density of short, numeric/label lines relative to total = a table
    avg_line_len = sum(len(l) for l in lines) / len(lines)
    return numeric_lines / len(lines) > 0.4 and avg_line_len < 80

TOPIC_DEDUP_SYSTEM_INSTRUCTION = (
    "You are reviewing a list of topic names extracted from one course's lecture "
    "slides. Because extraction ran in separate chunks, some topics may be "
    "near-duplicates — different wording for substantially the same material "
    "(e.g. 'Introduction to X' and 'X for Y Case' when both actually teach the "
    "same core concept and worked examples).\n\n"
    "Your job: group any topics that teach SUBSTANTIALLY the same material, and "
    "for each group, pick or write ONE canonical name to represent it — prefer "
    "the clearest, most specific existing name in the group, not a vague umbrella "
    "title.\n\n"
    "Rules:\n"
    "1. Only merge topics you're confident overlap — a legitimately distinct "
    "sub-topic (e.g. 'IPR for Two-Phase Reservoirs' vs 'IPR for Partial Two-Phase "
    "Reservoirs' vs 'Constructing IPR Curves from Field Test Data') must stay "
    "separate, even if related, if each covers genuinely different content or "
    "worked examples.\n"
    "2. Do not merge topics just because they share a subject area — only merge "
    "when they would produce near-identical lecture content.\n"
    "3. A topic with no duplicates stays exactly as given, unchanged.\n"
    "4. Return ONLY a JSON object mapping EVERY original topic name (as given) "
    "to its canonical name after merging. Topics with no duplicate map to "
    "themselves. No markdown fences, no explanation.\n\n"
    'Example: {"Introduction to X": "X Fundamentals", "X Fundamentals": '
    '"X Fundamentals", "Y Basics": "Y Basics"}'
)


def dedup_topics_semantically(slide):
    """Collapses near-duplicate topic names in slide.extracted_topics into
    canonical names, preserving list order (first occurrence of each
    canonical name wins its position). Returns the deduplicated list and
    saves it onto the slide. Safe to call on a slide with no duplicates —
    the mapping will just be identity for every topic."""
    topics = list(slide.extracted_topics or [])
    if len(topics) < 2:
        return topics

    client = _get_batch_client()
    topics_list_str = "\n".join(f"- {t}" for t in topics)

    raw = _call_gemini_with_retry(
        client=client,
        model="gemini-3.6-flash",
        contents=f"Course: {slide.course_code} — {slide.course_title}\n\nTopics:\n{topics_list_str}",
        config=types.GenerateContentConfig(
            system_instruction=TOPIC_DEDUP_SYSTEM_INSTRUCTION,
            max_output_tokens=8000,
        ),
        course_code=slide.course_code,
        chunk_label="Topic dedup pass",
    )

    if not raw or not raw.strip():
        print(f"[{slide.course_code}] Topic dedup: no response, keeping topics as-is.")
        return topics

    clean = raw.replace("```json", "").replace("```", "").strip()
    a, b = clean.find("{"), clean.rfind("}")
    if a == -1 or b <= a:
        print(f"[{slide.course_code}] Topic dedup: unparseable response, keeping topics as-is.")
        return topics

    try:
        mapping = json.loads(clean[a:b + 1])
    except json.JSONDecodeError:
        print(f"[{slide.course_code}] Topic dedup: JSON decode failed, keeping topics as-is.")
        return topics

    if not isinstance(mapping, dict):
        return topics

    canonical_order = []
    seen = set()
    for t in topics:
        canon = mapping.get(t, t)
        if not isinstance(canon, str) or not canon.strip():
            canon = t  # bad/missing mapping — fall back to original, never drop a topic
        if canon not in seen:
            seen.add(canon)
            canonical_order.append(canon)

    merged_count = len(topics) - len(canonical_order)
    if merged_count > 0:
        print(f"[{slide.course_code}] Topic dedup: merged {merged_count} duplicate topic(s), "
              f"{len(topics)} → {len(canonical_order)}.")
        slide.extracted_topics = canonical_order
        slide.save(update_fields=["extracted_topics"])
    else:
        print(f"[{slide.course_code}] Topic dedup: no duplicates found.")

    return canonical_order

def summarize_slide(slide):
    """Plain facts about how the slide pipeline went (used for the job result)."""
    pages = split_pages(slide.extracted_text or "")
    all_chunks = "\n".join(slide.topic_chunks.values_list("chunk_text", flat=True))
    placed = set(re.findall(r"^--- (.+?) ---[ \t]*$", all_chunks, re.MULTILINE))
    has_markers = bool(PAGE_MARKER_RE.search(slide.extracted_text or ""))
    return {
        "topics": len(slide.extracted_topics or []),
        "topics_incomplete": bool(slide.topics_incomplete),
        "empty_topics": list(slide.topic_chunks.filter(is_empty=True).values_list("topic_name", flat=True)),
        "cleanup_fallbacks": slide.cleanup_chunks.exclude(error_message="").count(),
        "split_failed": slide.topic_split_chunks.filter(status="FAILED").count(),
        "unplaced_pages": [p.label for p in pages
                           if has_markers and p.label not in placed and _is_content_page(p)],
    }