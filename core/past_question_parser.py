import json
from google import genai
from google.genai import types
from django.conf import settings
from .slide_extractor import extract_text_from_slide


def parse_past_questions_with_ai(course_code, course_title, raw_text):
    """Use Gemini to parse uploaded past questions into structured JSON"""
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

    response = client.models.generate_content(
        model="gemini-3.7-flash",
        contents=prompt,
        config=types.GenerateContentConfig(max_output_tokens=6000),
    )
    raw = response.text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def _parse_past_question_file(pq_obj):
    """Extract text and parse questions from uploaded file"""
    text = extract_text_from_slide(pq_obj.file.path, course_code=pq_obj.course_code)
    pq_obj.extracted_text = text
    pq_obj.parsed = True
    pq_obj.save()  # text saved immediately, independent of AI outcome

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