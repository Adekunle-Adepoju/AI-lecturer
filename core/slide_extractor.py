import os


def extract_text_via_vision(file_path, course_code="", max_pages=15):
    """
    Render PDF pages to images and transcribe them using Gemini vision.
    Default path for ALL PDFs — handles both scanned/handwritten and
    text-based PDFs uniformly, per project decision.
    """
    import fitz  # PyMuPDF
    from google import genai
    from google.genai import types
    from django.conf import settings

    api_key = getattr(settings, "GEMINI_API_KEY_EXTRACTION", None)
    if not api_key:
        raise ValueError("GEMINI_API_KEY_EXTRACTION is missing from Django settings.py.")

    client = genai.Client(api_key=api_key)
    doc = fitz.open(file_path)

    if len(doc) > max_pages:
        print(f"Warning: {course_code} has {len(doc)} pages, only processing first {max_pages} for cost control.")

    full_text = []
    for page_num in range(min(len(doc), max_pages)):
        page = doc[page_num]
        pix = page.get_pixmap(dpi=150)
        image_bytes = pix.tobytes("png")

        try:
            response = client.models.generate_content(
                model="gemini-3.7-flash",
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                    "Transcribe all text/handwriting on this page exactly as written, "
                    "including formulas, headings, and diagram labels. Preserve structure "
                    "with line breaks. If a formula is written by hand, transcribe it as "
                    "plain text math notation. Do not summarize or explain — transcribe only.",
                ],
                config=types.GenerateContentConfig(max_output_tokens=2000),
            )
            full_text.append(f"--- Page {page_num + 1} ---\n{response.text.strip()}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            full_text.append(f"--- Page {page_num + 1} (transcription failed) ---")

    doc.close()
    return "\n\n".join(full_text)


def extract_text_from_slide(file_path, course_code=""):
    """Extract text from uploaded PDF, DOCX, or PPTX file"""
    path = str(file_path)

    if path.endswith(".pdf"):
        return extract_text_via_vision(path, course_code=course_code)

    elif path.endswith(".docx"):
        import docx
        doc = docx.Document(path)
        text = "\n".join([para.text for para in doc.paragraphs if para.text.strip()])
        return text.strip()

    elif path.endswith(".pptx"):
        from pptx import Presentation
        prs = Presentation(path)
        extracted_chunks = []

        def extract_from_shape(shape):
            """Helper function to handle shapes, tables, and grouped elements"""
            # 1. Text boxes & auto shapes
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    para_text = paragraph.text.strip()
                    if para_text:
                        extracted_chunks.append(para_text)

            # 2. Tables
            elif shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        cell_text = cell.text.strip()
                        if cell_text:
                            extracted_chunks.append(cell_text)

            # 3. Grouped shapes (recursive search)
            elif shape.shape_type == 6:  # MSO_SHAPE_TYPE.GROUP
                for sub_shape in shape.shapes:
                    extract_from_shape(sub_shape)

        for slide in prs.slides:
            for shape in slide.shapes:
                extract_from_shape(shape)

        return "\n".join(extracted_chunks).strip()

    else:
        return ""