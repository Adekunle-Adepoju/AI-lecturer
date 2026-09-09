import json
import time
from google import genai
from google.genai import types
from django.conf import settings
from .slide_extractor import extract_text_from_slide


def parse_past_questions_with_ai(course_code, course_title, raw_text):
    """Use Gemini to parse uploaded past questions into structured JSON,
    with 429-aware retries."""
    client = genai.Client(api_key=settings.GEMINI_API_KEY_EXTRACTION)

    prompt = f"""You are given raw text extracted from a past exam/test paper for {course_code} — {course_title}.

Extract every multiple choice question you can find. For each question, identify:
- The question text
- All answer options (label them A, B, C, D)
- The correct answer index if indicated, otherwise your best expert judgment (0 for A, 1 for B, 2 for C, 3 for D)
- A short explanation of why that answer is correct
- A "topic_hint" — a short phrase describing what topic/concept this question tests

If the source isn't multiple choice (e.g. theory/essay questions), convert it into a multiple choice format
that tests the same underlying concept, with 4 plausible options.

Return ONLY a JSON array, no explanation, no markdown fences. Format:
[
  {{
    "question": "question text",
    "options": ["A. ...", "B. ...", "C. ...", "D. ..."],
    "correct_index": 0,
    "explanation": "why this is correct",
    "topic_hint": "short topic description"
  }}
]

Extract as many questions as you can find, up to 40.

Raw text:
{raw_text[:8000]}
"""

    max_retries = 3
    raw = None
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
                config=types.GenerateContentConfig(max_output_tokens=6000),
            )
            raw = response.text
            break
        except Exception as e:
            error_str = str(e)
            is_rate_limit = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
            if is_rate_limit:
                print(f"[{course_code}] Past-question parse: 429 hit (attempt {attempt + 1}/{max_retries}) — sleeping 20s and retrying...")
                time.sleep(20)
                continue
            print(f"[{course_code}] Past-question parse: error ({e}), attempt {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                time.sleep(3)
                continue
            raise

    if not raw or not raw.strip():
        raise ValueError("Empty response from extraction client while parsing past questions")

    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def _parse_past_question_file(pq_obj):
    """Extract text and parse questions from uploaded file"""
    text = extract_text_from_slide(pq_obj.file.path, course_code=pq_obj.course_code)
    pq_obj.extracted_text = text
    pq_obj.parsed = True
    pq_obj.save()

    try:
        questions = parse_past_questions_with_ai(
            pq_obj.course_code,
            pq_obj.course_title,
            text
        )
        pq_obj.parsed_questions = questions
        pq_obj.save()
    except Exception as e:
        import traceback
        traceback.print_exc()
        # parsed_questions stays [] (the field default) if AI extraction fails