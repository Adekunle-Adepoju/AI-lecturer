import json
import re
from groq import Groq
from django.conf import settings


def extract_text_from_file(file_path):
    """Extract text from PDF or DOCX"""
    path = str(file_path)

    if path.endswith(".pdf"):
        import fitz  # pymupdf
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
        # Try reading as plain text
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()


def parse_outline_with_ai(course_code, course_title, outline_text):
    """Use Groq to parse the outline into 15 weeks x 3 topics"""
    client = Groq(api_key=settings.GROQ_API_KEY)

    prompt = f"""You are given a course outline for {course_code} — {course_title}.
Parse it into exactly 15 weeks, with exactly 3 specific topics per week.
The topics must follow the order in the outline.
If the outline has fewer than 45 topics total, expand reasonably within the subject area.

Return ONLY a JSON object like this — no explanation, no markdown, no preamble:
{{
  "1": ["Topic 1", "Topic 2", "Topic 3"],
  "2": ["Topic 4", "Topic 5", "Topic 6"],
  ...
  "15": ["Topic 43", "Topic 44", "Topic 45"]
}}

Course outline text:
{outline_text[:6000]}
"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=3000,
        messages=[
            {"role": "user", "content": prompt}
        ],
    )

    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def _parse_course_outline(outline_obj):
    """Extract text and parse topics from uploaded outline file"""
    from .models import CourseOutline

    # Extract text
    text = extract_text_from_file(outline_obj.file.path)
    outline_obj.extracted_text = text

    # Parse into weekly topics using AI
    topics_json = parse_outline_with_ai(
        outline_obj.course_code,
        outline_obj.course_title,
        text
    )

    outline_obj.topics_json = topics_json
    outline_obj.parsed = True
    outline_obj.save()