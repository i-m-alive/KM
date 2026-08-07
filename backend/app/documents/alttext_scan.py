"""Image alt-text (descr/title/name) scanning.

This sits between the two existing verification channels and closes a real
seam between them: the image channel (image_scan.py) re-scans PIXELS; the
text channel (verify.py) scans body RUN text. A picture's non-visual drawing
properties - `p:cNvPr` in PPTX, `wp:docPr` in DOCX, `xdr:cNvPr` in XLSX -
carry human-typed alt-text in `descr`/`title` (and occasionally a meaningful
`name`) that neither channel ever reaches. Observed on a real produced file:
21 client names recovered from `descr="..."` attributes on logos whose PIXELS
had already been fully redacted - the label survived the redaction.

Element matched by LOCAL NAME (cNvPr / docPr), not full qualified name or
parent context: every shape type (picture, text box, group, graphic frame,
connector) uses the same non-visual-properties tag regardless of what kind
of shape it's attached to, so one pattern covers all of them uniformly,
without needing to enumerate every shape-type wrapper element.

Only `descr`/`title` are extracted here (not `name`) for feeding into
detect()'s candidate pipeline - a default shape name like "Picture 3" carries
no PII and would just add noise. `name` IS still covered by the scrub side
(alttext_scrub.py) and by verify's residual check, as defense in depth for
the rare producer that puts something meaningful there instead.
"""

import re
import zipfile
from xml.sax.saxutils import unescape

from app.masking.pattern import surface_pattern

# Captures: (1) tag name incl. prefix, (2) attribute string, (3) "/" if self-closing.
TAG_RE = re.compile(r"<((?:\w+:)?(?:cNvPr|docPr))\b([^>]*?)(/?)>")
_DESCR_TITLE_RE = re.compile(r'\b(descr|title)=(["\'])((?:(?!\2).)*)\2')
_DESCR_TITLE_NAME_RE = re.compile(r'\b(descr|title|name)=(["\'])((?:(?!\2).)*)\2')

_PPTX_PARTS = re.compile(r"^ppt/(slides|slideLayouts|slideMasters|notesSlides)/[^/]+\.xml$")
_DOCX_PARTS = re.compile(r"^word/(document|header\d*|footer\d*)\.xml$")
_XLSX_PARTS = re.compile(r"^xl/drawings/drawing\d*\.xml$")


def target_parts(z: zipfile.ZipFile, content_type: str, filename: str) -> list[str]:
    lower = filename.lower()
    if lower.endswith(".pptx") or "presentationml" in content_type:
        pattern = _PPTX_PARTS
    elif lower.endswith(".docx") or "wordprocessingml" in content_type:
        pattern = _DOCX_PARTS
    elif lower.endswith(".xlsx") or "spreadsheetml" in content_type:
        pattern = _XLSX_PARTS
    else:
        return []
    return [n for n in z.namelist() if pattern.match(n)]


def _values_in_part(xml_text: str, include_name: bool) -> list[str]:
    attr_re = _DESCR_TITLE_NAME_RE if include_name else _DESCR_TITLE_RE
    values = []
    for tag_match in TAG_RE.finditer(xml_text):
        for attr_match in attr_re.finditer(tag_match.group(2)):
            v = unescape(attr_match.group(3)).strip()
            if v:
                values.append(v)
    return values


def extract_alt_text(path: str, content_type: str, filename: str, include_name: bool = False) -> list[str]:
    """Every non-empty descr/title (and name, if include_name) value on a
    cNvPr/docPr element, across every relevant part in the package. Used to
    feed alt-text-only names into detect()'s candidate pipeline, and by
    verify.py's text channel to catch a residual after masking."""
    try:
        with zipfile.ZipFile(path) as z:
            parts = target_parts(z, content_type, filename)
            values: list[str] = []
            for name in parts:
                try:
                    text = z.read(name).decode("utf-8")
                except UnicodeDecodeError:
                    continue
                values.extend(_values_in_part(text, include_name))
            return values
    except Exception:
        return []


def find_residual_alt_text(path: str, content_type: str, filename: str, surfaces: list[str]) -> list[str]:
    """Subset of `surfaces` that still appear in any descr/title/name value."""
    if not surfaces:
        return []
    values = extract_alt_text(path, content_type, filename, include_name=True)
    if not values:
        return []
    joined = "\n".join(values)
    return [s for s in surfaces if re.search(surface_pattern(s), joined, flags=re.IGNORECASE)]
