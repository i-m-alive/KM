"""Structural extraction + chunking for uploaded documents.

Chunks preserve structure (page for PDF, paragraph-group for DOCX) so a location
stays meaningful and Sanitization can apply masks by exact character offset.
"""

from dataclasses import dataclass

from app.config import get_settings
from app.documents.pptx_walk import iter_shapes_recursive

settings = get_settings()


@dataclass
class Chunk:
    chunk_id: int
    kind: str  # "page" | "paragraph_group" | "slide" | "sheet"
    label: str  # human-readable, e.g. "page 3"
    text: str


class UnsupportedDocumentError(Exception):
    pass


def _extract_pdf(path: str) -> list[Chunk]:
    import pdfplumber

    chunks: list[Chunk] = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            chunks.append(Chunk(chunk_id=i, kind="page", label=f"page {i + 1}", text=text))
    return chunks


def _pptx_table_lines(table) -> list[str]:
    lines = []
    for row in table.rows:
        cells = [c.text.strip() for c in row.cells if c.text.strip()]
        if cells:
            lines.append(" | ".join(cells))
    return lines


def _extract_pptx(path: str) -> list[Chunk]:
    from pptx import Presentation

    prs = Presentation(path)
    chunks: list[Chunk] = []
    for i, slide in enumerate(prs.slides):
        lines: list[str] = []
        # Recurse into grouped shapes - a plain top-level loop misses any text
        # nested inside a PowerPoint "Group" (very common for logo+label
        # graphics, org charts, diagrams).
        for shape in iter_shapes_recursive(slide.shapes):
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in para.runs).strip()
                    if text:
                        lines.append(text)
            elif getattr(shape, "has_table", False):
                lines.extend(_pptx_table_lines(shape.table))
        # Preserve slide boundaries so Deck Drafting (A-06) can reuse them later.
        chunks.append(Chunk(chunk_id=i, kind="slide", label=f"slide {i + 1}", text="\n".join(lines)))
    return chunks


def _docx_table_lines(table) -> list[str]:
    lines = []
    for row in table.rows:
        for cell in row.cells:
            for p in cell.paragraphs:
                if p.text.strip():
                    lines.append(p.text.strip())
            # Tables nested inside a cell (common in complex templates).
            for nested in cell.tables:
                lines.extend(_docx_table_lines(nested))
    return lines


def _extract_docx(path: str) -> list[Chunk]:
    import docx

    document = docx.Document(path)
    paragraphs = [p.text for p in document.paragraphs if p.text.strip()]

    # Headers/footers carry client names surprisingly often (letterhead-style
    # templates) and were previously invisible to the whole pipeline.
    header_footer_lines: list[str] = []
    for section in document.sections:
        for part in (section.header, section.footer):
            for p in part.paragraphs:
                if p.text.strip():
                    header_footer_lines.append(p.text.strip())

    # Tables at the document body level (and any tables nested within them).
    table_lines: list[str] = []
    for table in document.tables:
        table_lines.extend(_docx_table_lines(table))

    chunks: list[Chunk] = []
    if header_footer_lines:
        chunks.append(Chunk(chunk_id=-1, kind="header_footer", label="headers & footers", text="\n".join(header_footer_lines)))

    # Group body paragraphs into chunks so each LLM call sees a coherent block.
    per_chunk = max(1, settings.CHUNK_PARAGRAPHS)
    for i in range(0, len(paragraphs), per_chunk):
        group = paragraphs[i : i + per_chunk]
        start = i // per_chunk
        chunks.append(
            Chunk(
                chunk_id=start,
                kind="paragraph_group",
                label=f"paragraphs {i + 1}-{i + len(group)}",
                text="\n\n".join(group),
            )
        )

    if table_lines:
        chunks.append(Chunk(chunk_id=len(chunks), kind="tables", label="tables", text="\n".join(table_lines)))

    return chunks


def _extract_xlsx(path: str) -> list[Chunk]:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    chunks: list[Chunk] = []
    for i, sheet_name in enumerate(wb.sheetnames):
        ws = wb[sheet_name]
        lines: list[str] = []
        for row in ws.iter_rows():
            cells = [str(c.value).strip() for c in row if c.value is not None and str(c.value).strip()]
            if cells:
                lines.append(" | ".join(cells))
        chunks.append(Chunk(chunk_id=i, kind="sheet", label=f"sheet '{sheet_name}'", text="\n".join(lines)))
    return chunks


def extract_chunks(stored_path: str, content_type: str, filename: str) -> list[Chunk]:
    """Extract a document into structural chunks. Dispatches by content type / extension."""
    lower = filename.lower()
    if content_type == "application/pdf" or lower.endswith(".pdf"):
        return _extract_pdf(stored_path)
    if (
        content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or lower.endswith(".docx")
    ):
        return _extract_docx(stored_path)
    if (
        content_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        or lower.endswith(".pptx")
    ):
        return _extract_pptx(stored_path)
    if (
        content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        or lower.endswith(".xlsx")
    ):
        return _extract_xlsx(stored_path)
    raise UnsupportedDocumentError(
        f"Unsupported document type '{content_type}' ({filename}). Sanitization supports PDF, DOCX, PPTX, and XLSX."
    )
