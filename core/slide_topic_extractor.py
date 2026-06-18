import json
from groq import Groq
from django.conf import settings


def extract_topics_from_slide(course_code, course_title, slide_text):
    """Use Groq to extract ALL teachable topics from a full course slide deck, in order"""
    client = Groq(api_key=settings.GROQ_API_KEY)

    prompt = f"""You are given the FULL slide content for an entire course: {course_code} — {course_title}.

Extract EVERY distinct topic that should be taught from this material, in the exact order they appear.
Be thorough — include topics that may not be obvious headings but are clearly taught as separate concepts
(for example "Conduction Through Slabs" might appear under a broader heading but deserves its own topic entry).

Each topic name should be specific and teachable in one sitting — not too broad, not too narrow.
Aim for the natural granularity a lecturer would use when teaching session by session.

Return ONLY a JSON array of topic name strings, in teaching order, nothing else. No explanation, no markdown.
Example: ["Introduction to Heat Transfer", "Conduction in Solids", "Conduction Through Slabs", "Conduction Through Composite Walls", ...]

Extract as many topics as genuinely exist in the material — could be 10, could be 60. Do not artificially limit the count.

Slide content:
{slide_text[:10000]}
"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def _parse_slide_document(slide_obj):
    """Extract text and topics from an uploaded slide deck"""
    from .slide_extractor import extract_text_from_slide

    text = extract_text_from_slide(slide_obj.file.path)
    slide_obj.extracted_text = text

    topics = extract_topics_from_slide(
        slide_obj.course_code,
        slide_obj.course_title,
        text
    )

    slide_obj.extracted_topics = topics
    slide_obj.parsed = True
    slide_obj.save()