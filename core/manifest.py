# core/manifest.py
import json
from google.genai import types
from .gemini_calls import _get_batch_client, _call_gemini_with_retry  # adjust import to wherever these actually live

MANIFEST_SYSTEM_INSTRUCTION = (
    "You are building a coverage checklist from one topic's worth of "
    "university lecture slide text. Extract every distinct concept, "
    "definition, formula, or named sub-heading a lecturer would need to "
    "mention for a student to say the topic was fully taught — not a "
    "summary, a checklist. "
    "Return ONLY a JSON array of short strings (3-8 words each), nothing "
    "else. If the text is thin, return however many genuine items exist — "
    "do not pad the list. Example: "
    '["Definition of formation volume factor", "Governing equation for Bt", '
    '"Worked example: gas cap reservoir"]'
)


def extract_coverage_manifest(course_code, topic_name, slide_text):
    """Gemini-based manifest extraction, scoped to one topic's slide text.
    Falls back to an empty list (never crashes the caller) on any failure."""
    if not slide_text or not slide_text.strip():
        return []

    client = _get_batch_client()
    raw = _call_gemini_with_retry(
        client=client,
        model="gemini-3.6-flash",
        contents=f"Topic: {topic_name}\n\nSlide text:\n{slide_text}",
        config=types.GenerateContentConfig(
            system_instruction=MANIFEST_SYSTEM_INSTRUCTION,
            max_output_tokens=1000,
        ),
        course_code=course_code,
        chunk_label=f"Manifest — {topic_name}",
    )

    if not raw or not raw.strip():
        return []

    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip()

    try:
        items = json.loads(clean)
        return [i.strip() for i in items if isinstance(i, str) and i.strip()] if isinstance(items, list) else []
    except json.JSONDecodeError:
        return []


def format_manifest_for_prompt(manifest):
    if not manifest:
        return "(no explicit manifest extracted — cover all content in the slide excerpt thoroughly)"
    return "\n".join(f"- {item}" for item in manifest)