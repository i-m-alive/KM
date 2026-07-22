"""Detect-and-block check: does a masked surface still appear in document
metadata? (Company/Manager and custom properties are an easy place for a
client name to leak that no masking step below ever touches - this module
only DETECTS residual leaks; scrubbing metadata is a deliberate follow-up,
not implemented here.)

Separately, dc:creator/cp:lastModifiedBy/cp:manager (PDF: /Author) get their
own UNCONDITIONAL residual check, independent of `surfaces` - metadata_scrub
clears these regardless of whether that specific name was ever detected as a
client entity (see metadata_scrub.py's docstring for why), so verifying they
actually ARE cleared can't be gated on the same surface list either, or a
name nobody ever detected (the real observed case: "Vallab" in creator,
never flagged by anything) would silently pass every time.
"""

import re
import xml.etree.ElementTree as ET
import zipfile

from app.masking.pattern import surface_pattern

_IDENTITY_FIELD_LOCAL_NAMES = {"creator", "lastModifiedBy", "manager"}


def _local_name(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _all_text(xml_bytes: bytes) -> str:
    """Every text node in an XML part, joined - deliberately schema-agnostic
    (Company/Manager/custom-property tag names vary) so we don't need to
    track every possible property name individually."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return ""
    return " ".join(el.text.strip() for el in root.iter() if el.text and el.text.strip())


def _residual_identity_fields(xml_bytes: bytes) -> list[str]:
    """Unconditional: any of creator/lastModifiedBy/manager still non-empty
    is itself the leak, regardless of whether its value matches anything in
    `surfaces` - these fields are always a real identity by construction."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    hits = []
    for el in root.iter():
        local = _local_name(el.tag)
        if local in _IDENTITY_FIELD_LOCAL_NAMES and el.text and el.text.strip():
            hits.append(f"identity field '{local}' still populated: '{el.text.strip()}'")
    return hits


def _find_in_text(text: str, surfaces: list[str], where: str) -> list[str]:
    hits = []
    for surface in surfaces:
        if re.search(surface_pattern(surface), text, flags=re.IGNORECASE):
            hits.append(f"{where}: '{surface}'")
    return hits


def _scan_ooxml_docprops(path: str, surfaces: list[str]) -> list[str]:
    hits: list[str] = []
    try:
        with zipfile.ZipFile(path) as z:
            for partname, label in (("docProps/core.xml", "core properties"), ("docProps/app.xml", "app properties (e.g. Company/Manager)"), ("docProps/custom.xml", "custom document properties")):
                if partname not in z.namelist():
                    continue
                data = z.read(partname)
                hits.extend(_residual_identity_fields(data))
                if surfaces:
                    hits.extend(_find_in_text(_all_text(data), surfaces, label))
    except Exception:
        pass
    return hits


def _scan_pdf_metadata(path: str, surfaces: list[str]) -> list[str]:
    import fitz

    hits: list[str] = []
    doc = fitz.open(path)
    meta = doc.metadata or {}
    if meta.get("author"):
        hits.append(f"identity field 'author' still populated: '{meta['author']}'")
    if surfaces:
        info_text = " ".join(str(v) for v in meta.values() if v)
        hits.extend(_find_in_text(info_text, surfaces, "PDF Info dictionary"))
        try:
            xmp = doc.get_xml_metadata()
        except Exception:
            xmp = ""
        if xmp:
            hits.extend(_find_in_text(xmp, surfaces, "PDF XMP metadata"))
    doc.close()
    return hits


def find_residual_metadata(path: str, content_type: str, filename: str, surfaces: list[str]) -> list[str]:
    """Returns human-readable residual hits, empty if clean. Format-agnostic:
    docx/pptx/xlsx share the same docProps convention; PDF has its own.

    Deliberately does NOT early-return when `surfaces` is empty - the
    identity-field check (creator/lastModifiedBy/manager/PDF author) always
    runs, since it isn't gated on any specific detected surface."""
    lower = filename.lower()
    if content_type == "application/pdf" or lower.endswith(".pdf"):
        return _scan_pdf_metadata(path, surfaces)
    if (
        lower.endswith((".docx", ".pptx", ".xlsx"))
        or "wordprocessingml" in content_type
        or "presentationml" in content_type
        or "spreadsheetml" in content_type
    ):
        return _scan_ooxml_docprops(path, surfaces)
    return []
