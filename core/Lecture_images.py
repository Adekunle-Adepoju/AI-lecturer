import os
import uuid
from django.conf import settings
from google import genai

image_client = genai.Client(api_key=settings.GEMINI_API_KEY_IMAGES)

def generate_topic_image(description, course_code):
    """Generate a single explanatory image for a lecture topic.
    Returns a path relative to MEDIA_ROOT (e.g. 'lecture_images/xyz.png'), or None on failure."""
    try:
        response = image_client.models.generate_content(
            model="gemini-3.7-flash-image",
            contents=(
                f"Educational diagram for a university Petroleum & Gas Engineering student "
                f"studying {course_code}: {description}. Clear, labeled, textbook style, no watermark."
            ),
        )
        for part in response.candidates[0].content.parts:
            if part.inline_data:
                filename = f"lecture_images/{uuid.uuid4().hex}.png"
                full_path = os.path.join(settings.MEDIA_ROOT, filename)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "wb") as f:
                    f.write(part.inline_data.data)
                return filename
    except Exception:
        import traceback
        traceback.print_exc()
    return None