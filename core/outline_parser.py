import json
import re
import time
import traceback
from google import genai
from google.genai import types
from django.conf import settings
from .slide_topic_extractor import _macro_chunk_text
from .outline_generation import isolate_course_section, split_outcomes_and_contents, list_course_codes
import ssl
import httpx


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


OUTLINE_PARSE_MODELS = ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"]
_TRANSIENT = ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE",
              "timeout", "Timeout", "ConnectionError", "RemoteDisconnected")


_BUSY_MARKERS = ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE")
_NETWORK_MARKERS = ("SSL", "EOF occurred", "ConnectError", "ReadTimeout", "ConnectTimeout",
                    "RemoteProtocolError", "Connection reset", "Connection aborted", "timed out")


def _is_network_error(e):
    if isinstance(e, (httpx.TransportError, ssl.SSLError, ConnectionError, TimeoutError)):
        return True
    return any(m in str(e) for m in _NETWORK_MARKERS)


def _call_with_retry(client, prompt, max_output_tokens=4000):
    """Retries busy/rate-limit errors (long waits) and network drops (short
    waits), then falls back to the next model. Returns response.text.
    Anything else is raised immediately."""
    last_error = None
    for model in OUTLINE_PARSE_MODELS:
        for attempt in range(4):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(max_output_tokens=max_output_tokens),
                )
                return response.text
            except Exception as e:
                last_error = e
                s = str(e)
                if "PerDay" in s:
                    print(f"Outline parse: {model} daily quota used up, trying next model.")
                    break
                network = _is_network_error(e)
                busy = any(m in s for m in _BUSY_MARKERS)
                if not (network or busy):
                    raise
                if attempt == 3:
                    print(f"Outline parse: {model} still failing, trying next model.")
                    break
                wait = 3 * (2 ** attempt) if network else 15 * (2 ** attempt)   # 3/6/12s or 15/30/60s
                kind = "network drop" if network else "model busy"
                print(f"Outline parse: {model} {kind} (attempt {attempt + 1}/4), "
                      f"waiting {wait}s. Reason: {s[:150]}")
                time.sleep(wait)
    raise RuntimeError(f"Outline parse failed on all models. Last error: {str(last_error)[:300]}")


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
Course contents are often written as one paragraph. Treat each semicolon- or sentence-separated
item as ONE topic, keep its own wording as a clear title of at most 12 words, and treat anything
in brackets after it as part of that topic, never as separate topics.

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
    """Extracts THIS course's section from the uploaded file (a document may hold
    many courses), then parses topics from its Course Contents only."""
    text = extract_text_from_file(outline_obj.file.path)
    section = isolate_course_section(text, outline_obj.course_code)
    if section is None:
        found = ", ".join(list_course_codes(text)) or "none"
        raise ValueError(
            f"{outline_obj.course_code} was not found in this file. Course codes found: {found}. "
            f"The code on the course must match the heading in the outline."
        )

    _outcomes, contents = split_outcomes_and_contents(section)
    outline_obj.extracted_text = section       # outcomes + contents; the generator needs both
    # "parsed" must mean "topics were extracted". Keep it True only if good
    # topics already exist from an earlier successful parse.
    outline_obj.parsed = bool((outline_obj.topics_json or {}).get("topics"))
    outline_obj.save()

    # Errors now propagate, so the task and the retry button show the real reason.
    topics = parse_outline_with_ai(outline_obj.course_code, outline_obj.course_title, contents)
    if not _validate_topics_against_source(topics, contents):
        raise ValueError(
            f"{outline_obj.course_code}: the topics the AI returned don't match the outline text. "
            f"Nothing was saved. Try again."
        )

    outline_obj.topics_json = {"topics": topics}
    outline_obj.parsed = True
    outline_obj.save()