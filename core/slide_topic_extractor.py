import json
from google import genai
from google.genai import types
from django.conf import settings



def extract_topics_from_slide(course_code, course_title, slide_text):
    """Use Gemini to extract ALL teachable topics from a full course slide deck, in order"""
    api_key = getattr(settings, "GEMINI_API_KEY_EXTRACTION", None)
    if not api_key:
        raise ValueError("GEMINI_API_KEY_EXTRACTION is missing from Django settings.py.")

    client = genai.Client(api_key=api_key)

    prompt = f"""You are given the FULL slide content for an entire course: {course_code} — {course_title}.

Extract EVERY distinct topic that should be taught from this material, in the exact order they appear.
Be thorough — include topics that may not be obvious headings but are clearly taught as separate concepts.

Each topic name should be specific and teachable in one sitting — not too broad, not too narrow.

Return ONLY a JSON array of topic name strings, in teaching order, nothing else. No explanation, no markdown.
Example: ["Introduction to Fluid Flow", "Porosity and Permeability", "Darcy's Law", ...]

Extract as many topics as genuinely exist in the material — could be 10, could be 60. Do not artificially limit the count.

Slide content:
{slide_text[:10000]}
"""

    response = client.models.generate_content(
        model="gemini-3.7-flash",
        contents=prompt,
        config=types.GenerateContentConfig(max_output_tokens=4000),
    )

    raw = response.text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def _parse_slide_document(slide_obj):
    """Extract text and topics from an uploaded slide deck"""
    from .slide_extractor import extract_text_from_slide

    # Pure text extraction — no AI, always saved regardless of what follows.
    text = extract_text_from_slide(slide_obj.file.path, course_code=slide_obj.course_code)
    slide_obj.extracted_text = text
    slide_obj.parsed = True
    slide_obj.save()

    # AI topic extraction is best-effort — falls back to hardcoded
    # COURSE_OUTLINES in _get_topics_for_week() if this fails.
    try:
        topics = extract_topics_from_slide(
            slide_obj.course_code,
            slide_obj.course_title,
            text
        )
        slide_obj.extracted_topics = topics
        slide_obj.save()
    except Exception as e:
        import traceback
        traceback.print_exc()