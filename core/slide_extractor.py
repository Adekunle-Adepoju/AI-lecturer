import os


def extract_text_via_vision(file_path, course_code="", max_pages=5):
    import fitz
    import tempfile
    import time
    from google import genai
    from google.genai import types
    from django.conf import settings

    api_key = getattr(settings, "GEMINI_API_KEY_EXTRACTION", None)
    if not api_key:
        raise ValueError("GEMINI_API_KEY_EXTRACTION is missing from settings.")

    client = genai.Client(api_key=api_key)

    doc = fitz.open(file_path)
    total_pages = len(doc)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
        temp_path = temp_pdf.name

    if total_pages > max_pages:
        print(f"[{course_code}] {total_pages} pages — trimming to {max_pages}.")
        trimmed = fitz.open()
        trimmed.insert_pdf(doc, from_page=0, to_page=max_pages - 1)
        trimmed.save(temp_path)
        trimmed.close()
    else:
        doc.save(temp_path)
    doc.close()

    gemini_file = None
    transcription = ""

    try:
        # Upload with retry
        for attempt in range(3):
            try:
                print(f"[{course_code}] Upload attempt {attempt + 1}...")
                with open(temp_path, "rb") as f:
                    gemini_file = client.files.upload(file=f, config={"mime_type": "application/pdf"})
                print(f"[{course_code}] Upload successful.")
                break
            except Exception as e:
                print(f"[{course_code}] Upload attempt {attempt + 1} failed: {e}")
                if attempt < 2:
                    time.sleep(3)
                else:
                    raise

        print(f"[{course_code}] Transcribing...")

        for model in ["gemini-3.6-flash", "gemini-3.7-flash"]:
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=[
                        gemini_file,
                        "Transcribe all text and handwriting in this document exactly as written, "
                        "page by page, including formulas, headings, and diagram labels. "
                        "Preserve structure with line breaks. Mark each page as '--- Page N ---'. "
                        "For handwritten formulas, use plain text math notation. "
                        "Do not summarize or explain — transcribe only.",
                    ],
                    config=types.GenerateContentConfig(max_output_tokens=8000),
                )
                transcription = response.text.strip()
                print(f"[{course_code}] Transcription done using {model} ({len(transcription)} chars).")
                break
            except Exception as e:
                print(f"[{course_code}] Model {model} failed: {e}. Trying next...")

        if not transcription:
            raise RuntimeError("All vision models failed to transcribe the document.")

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        if gemini_file:
            try:
                client.files.delete(name=gemini_file.name)
            except Exception as e:
                print(f"[{course_code}] Could not delete remote file: {e}")

    return transcription


def extract_text_from_slide(file_path, course_code=""):
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
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    para_text = paragraph.text.strip()
                    if para_text:
                        extracted_chunks.append(para_text)
            elif shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        cell_text = cell.text.strip()
                        if cell_text:
                            extracted_chunks.append(cell_text)
            elif shape.shape_type == 6:
                for sub_shape in shape.shapes:
                    extract_from_shape(sub_shape)

        for slide in prs.slides:
            for shape in slide.shapes:
                extract_from_shape(shape)

        return "\n".join(extracted_chunks).strip()

    else:
        return ""