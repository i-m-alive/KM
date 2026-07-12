"""Metadata scrubbing: rewrite core/app/custom document properties (and PDF
Info/XMP metadata) so a masked surface doesn't survive there even though it
was never part of any text run - a client name in "Company" or a custom
property is a real leak that render.py's text-run masking never touches.

Operates on the ALREADY-RENDERED masked file, in place - same "render, then
clean the rest" sequencing as image redaction. Uses the exact same three
OOXML docProps parts (core.xml, app.xml, custom.xml) that
app.documents.metadata_scan's detect-and-block check reads, so what this
scrubs and what that verifies are the same surface.
"""

import re
import shutil
import xml.etree.ElementTree as ET
import zipfile

from app.masking.pattern import surface_pattern
from app.masking.style import replacement_for

_OOXML_TARGETS = ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml")


def _substitute(text: str, surface_to_token: dict[str, str], style: str) -> str:
    out = text
    for surface in sorted(surface_to_token.keys(), key=len, reverse=True):
        token = surface_to_token[surface]
        out = re.sub(
            surface_pattern(surface), lambda m: replacement_for(m.group(0), token, style), out, flags=re.IGNORECASE
        )
    return out


def _scrub_xml_part(data: bytes, surface_to_token: dict[str, str], style: str) -> tuple[bytes, int]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return data, 0
    changed = 0
    for el in root.iter():
        if el.text and el.text.strip():
            new_text = _substitute(el.text, surface_to_token, style)
            if new_text != el.text:
                el.text = new_text
                changed += 1
    if not changed:
        return data, 0
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True), changed


def _scrub_ooxml(path: str, surface_to_token: dict[str, str], style: str) -> int:
    tmp_path = path + ".metatmp"
    total_changed = 0
    with zipfile.ZipFile(path, "r") as zin:
        names = zin.namelist()
        replacements: dict[str, bytes] = {}
        for name in _OOXML_TARGETS:
            if name not in names:
                continue
            new_bytes, changed = _scrub_xml_part(zin.read(name), surface_to_token, style)
            if changed:
                replacements[name] = new_bytes
                total_changed += changed
        if not replacements:
            return 0
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename in replacements:
                    zout.writestr(item, replacements[item.filename])
                else:
                    zout.writestr(item, zin.read(item.filename))
    shutil.move(tmp_path, path)
    return total_changed


def _scrub_pdf(path: str, surface_to_token: dict[str, str], style: str) -> int:
    import fitz

    doc = fitz.open(path)
    changed = 0
    meta = dict(doc.metadata or {})
    new_meta = dict(meta)
    for k, v in meta.items():
        if isinstance(v, str) and v:
            new_v = _substitute(v, surface_to_token, style)
            if new_v != v:
                new_meta[k] = new_v
                changed += 1
    if changed:
        doc.set_metadata(new_meta)

    try:
        xmp = doc.get_xml_metadata()
    except Exception:
        xmp = ""
    if xmp:
        new_xmp = _substitute(xmp, surface_to_token, style)
        if new_xmp != xmp:
            doc.set_xml_metadata(new_xmp)
            changed += 1

    if changed:
        doc.saveIncr()
    doc.close()
    return changed


def scrub_metadata(path: str, content_type: str, filename: str, surface_to_token: dict[str, str], style: str) -> int:
    """In-place. Returns the number of properties changed."""
    if not surface_to_token:
        return 0
    lower = filename.lower()
    if content_type == "application/pdf" or lower.endswith(".pdf"):
        return _scrub_pdf(path, surface_to_token, style)
    if (
        lower.endswith((".docx", ".pptx", ".xlsx"))
        or "wordprocessingml" in content_type
        or "presentationml" in content_type
        or "spreadsheetml" in content_type
    ):
        return _scrub_ooxml(path, surface_to_token, style)
    return 0
