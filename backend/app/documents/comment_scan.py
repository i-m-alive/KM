"""Detect-and-block check: does a masked surface still appear in a comment or
in track-changes history? Deliberately detect-only - scrubbing comments/
track-changes is a follow-up, not implemented here.

Deleted-but-still-present text in a tracked change lives in <w:delText>, not
<w:t> - ordinary paragraph/run text (and therefore the whole text-masking
pipeline) never sees it at all, so this is a genuinely separate channel, not
redundant with the text verifier.

PowerPoint modern/cloud (threaded) comments are checked alongside legacy
ones (see _modern_comment_parts / _modern_author_part) - per the MS-PPTX
spec, the modern Comment part's relationship type and content type are
normatively fixed (schemas.microsoft.com/office/2018/10/relationships/
comments), but its on-disk PATH is a producer choice, unlike legacy comments'
fixed ppt/comments/commentN.xml naming - so it's resolved via the
relationship graph, not a filename guess. Comment AUTHOR display names
(legacy ppt/commentAuthors.xml and its modern equivalent) are themselves PII
and are checked too.

Known, deliberate gap: Excel threaded comments (only legacy xlsx cell
comments are checked).
"""

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

from app.masking.pattern import surface_pattern

_MODERN_COMMENTS_RELTYPE = "http://schemas.microsoft.com/office/2018/10/relationships/comments"
_MODERN_AUTHORS_RELTYPE = "http://schemas.microsoft.com/office/2018/10/relationships/authors"


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


def _author_attr_text(z: zipfile.ZipFile, partname: str) -> str:
    """Author display names/initials in a commentAuthors-shaped part - stored
    as XML ATTRIBUTES (name=, initials=), not element text, on both the
    legacy (p:cmAuthor) and modern (2018-schema) author element - so this
    reads attribute values instead of _part_text's element-text walk.
    Generic over the child element's exact tag name (deliberately not
    hardcoded), since only the relationship type/content type of the modern
    Author part are spec-guaranteed, not its internal element names."""
    if partname not in z.namelist():
        return ""
    try:
        root = ET.fromstring(z.read(partname))
    except ET.ParseError:
        return ""
    values = []
    for el in root.iter():
        for attr in ("name", "initials"):
            v = el.get(attr)
            if v and v.strip():
                values.append(v.strip())
    return " ".join(values)


def _rels_targets(z: zipfile.ZipFile, rels_partname: str, reltype: str, base_dir: str) -> list[str]:
    """Every internal relationship target of `reltype` declared in a .rels
    part, resolved to an absolute in-zip path relative to `base_dir` (the
    folder the part described by the .rels belongs to, per OPC convention)."""
    if rels_partname not in z.namelist():
        return []
    try:
        root = ET.fromstring(z.read(rels_partname))
    except ET.ParseError:
        return []
    targets = []
    for rel in root:
        if rel.get("Type") == reltype and rel.get("TargetMode") != "External":
            target = rel.get("Target")
            if target:
                targets.append(posixpath.normpath(posixpath.join(base_dir, target)))
    return targets


def _modern_comment_parts(z: zipfile.ZipFile) -> list[str]:
    """Modern (2018-schema) per-slide comment parts, resolved via each
    slide's OWN relationships."""
    parts = []
    for slide_name in z.namelist():
        if not re.match(r"^ppt/slides/slide\d+\.xml$", slide_name):
            continue
        rels_name = f"ppt/slides/_rels/{posixpath.basename(slide_name)}.rels"
        parts.extend(_rels_targets(z, rels_name, _MODERN_COMMENTS_RELTYPE, "ppt/slides"))
    return parts


def _modern_author_part(z: zipfile.ZipFile) -> str | None:
    """At most one modern Author part per package, target of an implicit
    relationship from the Presentation part."""
    targets = _rels_targets(z, "ppt/_rels/presentation.xml.rels", _MODERN_AUTHORS_RELTYPE, "ppt")
    return targets[0] if targets else None


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
            if "ppt/commentAuthors.xml" in z.namelist():
                hits.extend(_find_in_text(_author_attr_text(z, "ppt/commentAuthors.xml"), surfaces, "comment author"))
            for name in _modern_comment_parts(z):
                hits.extend(_find_in_text(_part_text(z, name), surfaces, "comment"))
            author_part = _modern_author_part(z)
            if author_part:
                hits.extend(_find_in_text(_author_attr_text(z, author_part), surfaces, "comment author"))
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
