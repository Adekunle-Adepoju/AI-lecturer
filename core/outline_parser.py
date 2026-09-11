import json
import re
import time
import traceback
from google import genai
from google.genai import types
from django.conf import settings
from .slide_topic_extractor import _macro_chunk_text


def extract_text_from_file(file_path):
    """Extract text from PDF or DOCX"""
    path = str(file_path)
    if path.endswith(".pdf"):
        import fitz
        doc = fitz.open(path)
        text = ""
        for page in doc:
            text += page.get_text()
        return text
    elif path.endswith(".docx"):
        import docx
        doc = docx.Document(path)
        return "\n".join(para.text for para in doc.paragraphs)
    else:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()


def _call_with_retry(client, prompt, max_retries=3):
    """429-aware retry for a single outline-parsing call."""
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
                config=types.GenerateContentConfig(max_output_tokens=4000),
            )
            return response.text
        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                print(f"Outline parse: 429 hit (attempt {attempt + 1}/{max_retries}) — sleeping 20s...")
                time.sleep(20)
                continue
            print(f"Outline parse: error ({e}), attempt {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                time.sleep(3)
                continue
            raise
    raise RuntimeError("Outline parse: exhausted retries without success.")


def parse_outline_with_ai(course_code, course_title, outline_text):
    """Extract the topics actually present in the outline, in order.
    Never pads, never invents, never assumes a fixed week/topic shape."""
    api_key = getattr(settings, "GEMINI_API_KEY_EXTRACTION", None)
    if not api_key:
        raise ValueError("GEMINI_API_KEY_EXTRACTION is missing from Django settings.py.")

    client = genai.Client(api_key=api_key)
    chunks = _macro_chunk_text(outline_text, target_chunk_size=5000)
    all_topics = []

    for idx, chunk in enumerate(chunks):
        prompt = f"""You are given part {idx + 1} of {len(chunks)} of a course outline
for {course_code} — {course_title}.

Extract ONLY the topic names that are actually written in this text, in the
order they appear. Do NOT invent, expand, pad, or add topics that aren't
explicitly present in this excerpt — if this excerpt names 4 topics, return
exactly 4. If it names none, return an empty list.

Return ONLY a JSON array of topic name strings, nothing else.
Example: ["Topic A", "Topic B"]

Outline excerpt:
{chunk}
"""
        raw = _call_with_retry(client, prompt)
        clean = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)
        if isinstance(parsed, list):
            all_topics.extend(t for t in parsed if isinstance(t, str) and t.strip())
        time.sleep(2)

    return all_topics


def _validate_topics_against_source(topics, source_text, min_match_ratio=0.5):
    """Reject a topic list that doesn't actually seem to come from the
    source document."""
    if not topics:
        return False
    source_lower = source_text.lower()
    matches = sum(
        1 for t in topics
        if any(word.lower() in source_lower for word in re.findall(r"[A-Za-z]{5,}", t))
    )
    return (matches / len(topics)) >= min_match_ratio


def _parse_course_outline(outline_obj):
    """Extract text and parse topics from an uploaded outline file. Saves
    a flat, validated topic list — never week-keyed, never padded. If
    validation fails, topics_json stays empty and the app falls back to
    slide-extracted topics (the more reliable source in practice)."""
    text = extract_text_from_file(outline_obj.file.path)
    outline_obj.extracted_text = text
    outline_obj.parsed = True
    outline_obj.save()

    try:
        topics = parse_outline_with_ai(outline_obj.course_code, outline_obj.course_title, text)
        if _validate_topics_against_source(topics, text):
            outline_obj.topics_json = {"topics": topics}
            outline_obj.save()
        else:
            print(f"[{outline_obj.course_code}] Outline parse failed validation — "
                  f"leaving topics_json empty so the app falls back to slide-extracted topics.")
    except Exception:
        traceback.print_exc()