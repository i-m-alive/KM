"""Metadata scrubbing: rewrite core/app/custom document properties (and PDF
Info/XMP metadata) so a masked surface doesn't survive there even though it
was never part of any text run - a client name in "Company" or a custom
property is a real leak that render.py's text-run masking never touches.

Operates on the ALREADY-RENDERED masked file, in place - same "render, then
clean the rest" sequencing as image redaction. Uses the exact same three
OOXML docProps parts (core.xml, app.xml, custom.xml) that
app.documents.metadata_scan's detect-and-block check reads, so what this
scrubs and what that verifies are the same surface.

dc:creator / cp:lastModifiedBy / cp:manager get a SECOND, UNCONDITIONAL pass
on top of the surface-matching substitution above: Office populates these
from a real person's identity by construction, so leaving them dependent on
whether that specific name happened to be DETECTED as a client entity is
exactly the fragile pattern that let a real leak through ("Gaurav [CLIENT_5]"
- surname tokenized because it matched a detected entity, first name left
untouched because it never did; "Vallab" in creator, never detected at all,
left completely untouched). dc:title/dc:subject/cp:keywords/cp:company etc.
are NOT inherently identity-bearing (a title can legitimately be "Q3
Strategy Review") and correctly stay on the detection-dependent path only.
"""

import re
import shutil
import xml.etree.ElementTree as ET
import zipfile

from app.masking.pattern import surface_pattern
from app.masking.style import replacement_for

_OOXML_TARGETS = ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml")

# Local (namespace-stripped) tag names that are ALWAYS a real person's
# identity by construction, regardless of whether that specific name was
# ever detected/approved as a client entity - see module docstring.
_IDENTITY_FIELD_LOCAL_NAMES = {"creator", "lastModifiedBy", "manager"}


def _local_name(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


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
        if not (el.text and el.text.strip()):
            continue
        # Identity fields (creator/lastModifiedBy/manager) are cleared
        # UNCONDITIONALLY, before/instead of surface-matching - see module
        # docstring for why detection-dependent scrubbing isn't good enough
        # for a field that's always a real name by construction.
        if _local_name(el.tag) in _IDENTITY_FIELD_LOCAL_NAMES:
            el.text = ""
            changed += 1
            continue
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
    # PDF's Info /Author is the same "always a real identity" field as
    # dc:creator/cp:lastModifiedBy in OOXML - cleared unconditionally,
    # regardless of whether that name was ever detected as a client entity.
    if meta.get("author"):
        new_meta["author"] = ""
        changed += 1
    for k, v in meta.items():
        if k == "author":
            continue
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
    """In-place. Returns the number of properties changed.

    Deliberately does NOT early-return when surface_to_token is empty - the
    identity-field clearing (creator/lastModifiedBy/manager) is unconditional
    hygiene, not tied to whether anything was actually masked in this run."""
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
