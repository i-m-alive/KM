"""Hyperlink-target scrubbing: rewrite the actual href when a masked surface
appears in a hyperlink TARGET (e.g. https://www.acme.com), which the text-run
masking pass never touches - it only sees the visible display text.

Upgrades verified_hyperlinks from detect-and-block to detect-and-fix: this
runs on the ALREADY-RENDERED masked file in place (same sequencing as
metadata_scrub), and hyperlink_scan's verifier pass then re-checks the result,
still blocking if anything survived.

OOXML: hyperlink targets don't live in document.xml at all - they live as
Target="..." attributes on Relationship elements in the package's *.rels
parts (the main document, every header/footer, every slide, every worksheet
each has its own .rels). Scrubbing the rels XML directly in the zip covers
docx, pptx AND xlsx uniformly, and deliberately avoids openpyxl's
load->save round-trip, which renumbers media partnames and would corrupt the
image redactions already applied to the same file.
"""

import re
import shutil
import xml.etree.ElementTree as ET
import zipfile

from app.masking.pattern import surface_pattern
from app.masking.style import replacement_for

_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_HYPERLINK_RELTYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"


def _substitute(text: str, surface_to_token: dict[str, str], style: str) -> str:
    out = text
    for surface in sorted(surface_to_token.keys(), key=len, reverse=True):
        token = surface_to_token[surface]
        out = re.sub(
            surface_pattern(surface), lambda m: replacement_for(m.group(0), token, style), out, flags=re.IGNORECASE
        )
    return out


def _scrub_rels_part(data: bytes, surface_to_token: dict[str, str], style: str) -> tuple[bytes, int]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return data, 0
    changed = 0
    for rel in root.iter(f"{{{_RELS_NS}}}Relationship"):
        if rel.get("Type") != _HYPERLINK_RELTYPE or rel.get("TargetMode") != "External":
            continue
        target = rel.get("Target") or ""
        new_target = _substitute(target, surface_to_token, style)
        if new_target != target:
            rel.set("Target", new_target)
            changed += 1
    if not changed:
        return data, 0
    # Keep the default namespace unprefixed so the output matches what Office
    # itself writes (ET would otherwise emit ns0: prefixes).
    ET.register_namespace("", _RELS_NS)
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True), changed


def _scrub_ooxml(path: str, surface_to_token: dict[str, str], style: str) -> int:
    tmp_path = path + ".linktmp"
    total_changed = 0
    with zipfile.ZipFile(path, "r") as zin:
        replacements: dict[str, bytes] = {}
        for name in zin.namelist():
            if not name.endswith(".rels"):
                continue
            new_bytes, changed = _scrub_rels_part(zin.read(name), surface_to_token, style)
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
    for page in doc:
        for link in page.get_links():
            if link.get("kind") != fitz.LINK_URI or not link.get("uri"):
                continue
            new_uri = _substitute(link["uri"], surface_to_token, style)
            if new_uri != link["uri"]:
                link["uri"] = new_uri
                page.update_link(link)
                changed += 1
    if changed:
        doc.saveIncr()
    doc.close()
    return changed


def scrub_hyperlinks(path: str, content_type: str, filename: str, surface_to_token: dict[str, str], style: str) -> int:
    """In-place. Returns the number of hyperlink targets rewritten."""
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
