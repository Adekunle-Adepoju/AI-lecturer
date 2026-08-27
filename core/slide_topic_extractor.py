import json
from google import genai
from google.genai import types
from django.conf import settings


def extract_topics_from_slide(course_code, course_title, slide_text):
    api_key = getattr(settings, "GEMINI_API_KEY_EXTRACTION", None)
    if not api_key:
        raise ValueError("GEMINI_API_KEY_EXTRACTION is missing from settings.")

    client = genai.Client(api_key=api_key)

    prompt = f"""You are given the FULL slide content for an entire course: {course_code} — {course_title}.

Extract EVERY distinct topic that should be taught from this material, in the exact order they appear.
Each topic name should be specific and teachable in one sitting.

Return ONLY a JSON array of topic name strings, in teaching order, nothing else. No explanation, no markdown.
Example: ["Introduction to Fluid Flow", "Porosity and Permeability", "Darcy's Law"]

Slide content:
{slide_text[:10000]}
"""

    for model in ["gemini-3.6-flash", "gemini-3.7-flash"]:
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(max_output_tokens=4000),
            )
            raw = response.text.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            topics = json.loads(raw)
            print(f"[{course_code}] Topics extracted using {model}: {len(topics)} topics.")
            return topics
        except Exception as e:
            print(f"[{course_code}] Model {model} failed: {e}. Trying next...")

    raise RuntimeError(f"All models failed for topic extraction on {course_code}.")


def _parse_slide_document(slide_obj):
    from .slide_extractor import extract_text_from_slide

    print(f"[{slide_obj.course_code}] Starting text extraction...")

    try:
        text = extract_text_from_slide(slide_obj.file.path, course_code=slide_obj.course_code)
    except Exception as e:
        import traceback
        traceback.print_exc()
        slide_obj.extracted_text = ""
        slide_obj.parsed = True
        slide_obj.save()
        print(f"[{slide_obj.course_code}] Text extraction failed: {e}")
        return

    slide_obj.extracted_text = text
    slide_obj.parsed = True
    slide_obj.save()
    print(f"[{slide_obj.course_code}] Text saved ({len(text)} chars). Starting topic extraction...")

    try:
        topics = extract_topics_from_slide(
            slide_obj.course_code,
            slide_obj.course_title,
            text,
        )
        slide_obj.extracted_topics = topics
        slide_obj.save()
        print(f"[{slide_obj.course_code}] Done. {len(topics)} topics saved.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[{slide_obj.course_code}] Topic extraction failed: {e}. Text is still saved — use Retry Topics button.")