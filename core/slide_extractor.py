import os

def extract_text_from_slide(file_path):
    """Extract text from uploaded PDF, DOCX, or PPTX file"""
    path = str(file_path)

    if path.endswith(".pdf"):
        import fitz
        doc = fitz.open(path)
        text = ""
        for page in doc:
            text += page.get_text()
        return text.strip()

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