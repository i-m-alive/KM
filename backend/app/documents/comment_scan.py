"""Detect-and-block check: does a masked surface still appear in a comment or
in track-changes history? Deliberately detect-only - scrubbing comments/
track-changes is a follow-up, not implemented here.

Deleted-but-still-present text in a tracked change lives in <w:delText>, not
<w:t> - ordinary paragraph/run text (and therefore the whole text-masking
pipeline) never sees it at all, so this is a genuinely separate channel, not
redundant with the text verifier.

Known, deliberate gaps (unverified schemas, not worth guessing at): PowerPoint
modern/cloud coauthoring comments, and Excel threaded comments. Only legacy
comments are checked for pptx/xlsx.
"""

import re
import xml.etree.ElementTree as ET
import zipfile

from app.masking.pattern import surface_pattern


def _find_in_text(text: str, surfaces: list[str], where: str) -> list[str]:
    hits = []
    for surface in surfaces:
        if re.search(surface_pattern(surface), text, flags=re.IGNORECASE):
            hits.append(f"{where}: '{surface}'")
    return hits


def _part_text(z: zipfile.ZipFile, partname: str) -> str:
    if partname not in z.namelist():
        return ""
    try:
        root = ET.fromstring(z.read(partname))
    except ET.ParseError:
        return ""
    return " ".join(el.text.strip() for el in root.iter() if el.text and el.text.strip())


def _deltext_only(z: zipfile.ZipFile, partname: str) -> str:
    """Just the w:delText content of a part - the text a tracked deletion
    still carries, invisible to every other extraction path in this codebase."""
    if partname not in z.namelist():
        return ""
    try:
        root = ET.fromstring(z.read(partname))
    except ET.ParseError:
        return ""
    return " ".join(el.text.strip() for el in root.iter() if el.tag.endswith("delText") and el.text and el.text.strip())


def _scan_docx(path: str, surfaces: list[str]) -> list[str]:
    hits: list[str] = []
    try:
        with zipfile.ZipFile(path) as z:
            hits.extend(_find_in_text(_part_text(z, "word/comments.xml"), surfaces, "comment"))
            for partname in ("word/document.xml", "word/comments.xml"):
                hits.extend(_find_in_text(_deltext_only(z, partname), surfaces, "tracked deletion"))
    except Exception:
        pass
    return hits


def _scan_pptx(path: str, surfaces: list[str]) -> list[str]:
    hits: list[str] = []
    try:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if re.match(r"ppt/comments/comment\d*\.xml$", name):
                    hits.extend(_find_in_text(_part_text(z, name), surfaces, "comment"))
    except Exception:
        pass
    return hits


def _scan_xlsx_comments(path: str, surfaces: list[str]) -> list[str]:
    import openpyxl

    hits: list[str] = []
    wb = openpyxl.load_workbook(path)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.comment and cell.comment.text:
                    hits.extend(_find_in_text(cell.comment.text, surfaces, f"comment on {ws.title}!{cell.coordinate}"))
    return hits


def _scan_pdf(path: str, surfaces: list[str]) -> list[str]:
    import fitz

    hits: list[str] = []
    doc = fitz.open(path)
    for page_number, page in enumerate(doc):
        for annot in page.annots() or []:
            content = (annot.info or {}).get("content", "")
            if content:
                hits.extend(_find_in_text(content, surfaces, f"annotation on page {page_number + 1}"))
    doc.close()
    return hits


def find_residual_comments(path: str, content_type: str, filename: str, surfaces: list[str]) -> list[str]:
    if not surfaces:
        return []
    lower = filename.lower()
    if content_type == "application/pdf" or lower.endswith(".pdf"):
        return _scan_pdf(path, surfaces)
    if lower.endswith(".docx") or "wordprocessingml" in content_type:
        return _scan_docx(path, surfaces)
    if lower.endswith(".pptx") or "presentationml" in content_type:
        return _scan_pptx(path, surfaces)
    if lower.endswith(".xlsx") or "spreadsheetml" in content_type:
        return _scan_xlsx_comments(path, surfaces)
    return []
