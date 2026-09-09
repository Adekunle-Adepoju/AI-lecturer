import json
import traceback
import time
from google import genai
from google.genai import types
from django.conf import settings


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
        text = "\n".join([para.text for para in doc.paragraphs])
        return text

    else:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()


def parse_outline_with_ai(course_code, course_title, outline_text):
    """Use Gemini to parse the outline into 15 weeks x 3 topics, with
    429-aware retries."""
    api_key = getattr(settings, "GEMINI_API_KEY_EXTRACTION", None)
    if not api_key:
        raise ValueError("GEMINI_API_KEY_EXTRACTION is missing from Django settings.py.")

    client = genai.Client(api_key=api_key)

    prompt = f"""You are given a course outline for {course_code} — {course_title}.
Parse it into exactly 15 weeks, with exactly 3 specific topics per week.
The topics must follow the order in the outline.
If the outline has fewer than 45 topics total, expand reasonably within the subject area.

Return ONLY a JSON object like this — no explanation, no markdown, no preamble:
{{
  "1": ["Topic 1", "Topic 2", "Topic 3"],
  "2": ["Topic 4", "Topic 5", "Topic 6"],
  "15": ["Topic 43", "Topic 44", "Topic 45"]
}}

Course outline text:
{outline_text[:6000]}
"""

    max_retries = 3
    raw = None
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
                config=types.GenerateContentConfig(max_output_tokens=4000),
            )
            raw = response.text
            break
        except Exception as e:
            error_str = str(e)
            is_rate_limit = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
            if is_rate_limit:
                print(f"[{course_code}] Outline parse: 429 hit (attempt {attempt + 1}/{max_retries}) — sleeping 20s and retrying...")
                time.sleep(20)
                continue
            print(f"[{course_code}] Outline parse: error ({e}), attempt {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                time.sleep(3)
                continue
            raise  # non-rate-limit failure on final attempt — bubble up to caller's try/except

    if not raw or not raw.strip():
        raise ValueError("Empty response from extraction client while parsing outline")

    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def _parse_course_outline(outline_obj):
    """Extract text and parse topics from uploaded outline file"""
    text = extract_text_from_file(outline_obj.file.path)
    outline_obj.extracted_text = text
    outline_obj.parsed = True
    outline_obj.save()

    try:
        topics_json = parse_outline_with_ai(
            outline_obj.course_code,
            outline_obj.course_title,
            text
        )
        outline_obj.topics_json = topics_json
        outline_obj.save()
    except Exception as e:
        traceback.print_exc()
        # If AI fails, topics_json stays empty, but the upload and text are safe