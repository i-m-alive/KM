"""Comment / track-changes scrubbing: rewrite masked surfaces that survive in
comments or in tracked-deletion text (<w:delText>), which ordinary text-run
masking never sees. Runs on the ALREADY-RENDERED masked file in place, then
comment_scan's verifier pass re-checks the result and still blocks anything
that survived.

Implementation note: unlike the docProps parts (small, single-namespace),
word/document.xml carries mc:Ignorable prefix lists and many namespaces -
a full ElementTree parse->reserialize rewrites namespace prefixes and can
make Word reject the file. So OOXML parts are scrubbed with targeted regex
substitution on ELEMENT TEXT ONLY (<w:t>, <w:delText>, <p:text>, <t>),
leaving every byte of markup untouched. Comment AUTHOR display names are a
second shape entirely - stored as XML ATTRIBUTES (name=, initials=), not
element text - so those go through _scrub_attribute_values instead, same
text-preserving, no-reserialization approach applied to attribute values.

PowerPoint modern/cloud (threaded) comments are scrubbed alongside legacy
ones - see comment_scan's _modern_comment_parts/_modern_author_part, reused
here since the modern Comment/Author parts' on-disk paths aren't fixed by
the OOXML spec (only their relationship type is) and must be resolved the
same way for scrub as for scan, or the two could silently disagree about
which parts exist. The modern comment part's txBody is a standard
a:CT_TextBody (ISO/IEC29500-1 A.4.1) - the same a:t run-text tag used
everywhere else in DrawingML, not a bespoke element.

Deliberate gap, same as comment_scan: Excel threaded comments (legacy xlsx
cell comments only).
"""

import re
import shutil
import zipfile
from xml.sax.saxutils import escape, unescape

from app.documents.comment_scan import _modern_author_part, _modern_comment_parts
from app.masking.pattern import surface_pattern
from app.masking.style import replacement_for


def _substitute(text: str, surface_to_token: dict[str, str], style: str) -> str:
    out = text
    for surface in sorted(surface_to_token.keys(), key=len, reverse=True):
        token = surface_to_token[surface]
        out = re.sub(
            surface_pattern(surface), lambda m: replacement_for(m.group(0), token, style), out, flags=re.IGNORECASE
        )
    return out


def _scrub_element_text(xml_text: str, tags: list[str], surface_to_token: dict[str, str], style: str) -> tuple[str, int]:
    """Substitute masked surfaces inside the text content of the given element
    tags (e.g. 'w:delText'), touching nothing else in the markup. Text is
    XML-unescaped before matching and re-escaped after, so '&amp;'-style
    entities don't hide a surface from the word-boundary regex."""
    changed = 0

    def _sub(m: re.Match) -> str:
        nonlocal changed
        open_tag, inner, close_tag = m.group(1), m.group(2), m.group(3)
        raw = unescape(inner)
        new_raw = _substitute(raw, surface_to_token, style)
        if new_raw == raw:
            return m.group(0)
        changed += 1
        return open_tag + escape(new_raw) + close_tag

    out = xml_text
    for tag in tags:
        pattern = rf"(<{re.escape(tag)}(?:\s[^>]*)?>)((?:(?!</{re.escape(tag)}>).)*)(</{re.escape(tag)}>)"
        out = re.sub(pattern, _sub, out, flags=re.DOTALL)
    return out, changed


def _scrub_attribute_values(
    xml_text: str, attr_names: tuple[str, ...], surface_to_token: dict[str, str], style: str
) -> tuple[str, int]:
    """Substitute masked surfaces inside specific ATTRIBUTE values (e.g.
    name=, initials=) anywhere they appear in the raw XML text - comment
    author display names live here, not in element text, so
    _scrub_element_text's tag-body mechanism can't reach them. Generic over
    the enclosing element's tag name (deliberately not hardcoded - only the
    modern Author part's relationship/content type are spec-guaranteed, not
    its internal element names), and handles both single- and
    double-quoted attribute values."""
    changed = 0

    def _sub(m: re.Match) -> str:
        nonlocal changed
        attr, quote, value = m.group(1), m.group(2), m.group(3)
        raw = unescape(value)
        new_raw = _substitute(raw, surface_to_token, style)
        if new_raw == raw:
            return m.group(0)
        changed += 1
        return f"{attr}={quote}{escape(new_raw)}{quote}"

    names = "|".join(re.escape(a) for a in attr_names)
    pattern = rf'\b({names})=(["\'])((?:(?!\2).)*)\2'
    out = re.sub(pattern, _sub, xml_text)
    return out, changed


# Which element tags carry human-readable comment / tracked-change text,
# per OOXML part. document.xml only needs delText - its w:t runs were
# already masked by the normal render pass.
_PART_RULES: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"^word/comments\.xml$"), ["w:t", "w:delText"]),
    (re.compile(r"^word/document\.xml$"), ["w:delText"]),
    (re.compile(r"^ppt/comments/comment\d*\.xml$"), ["p:text"]),
    # Both xlsx comment layouts: classic Excel writes xl/comments1.xml;
    # openpyxl (and newer producers) write xl/comments/comment1.xml.
    (re.compile(r"^xl/comments\d*\.xml$"), ["t"]),
    (re.compile(r"^xl/comments/comment\d*\.xml$"), ["t"]),
]

# commentAuthors.xml (legacy p:cmAuthor / modern author elements) carries
# display names as ATTRIBUTES, not element text - handled separately from
# _PART_RULES via _scrub_attribute_values, not _scrub_element_text.
_AUTHOR_ATTRS = ("name", "initials")

_DRAWINGML_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _drawingml_prefix(xml_text: str) -> str:
    """The actual namespace prefix bound to DrawingML in THIS part. A modern
    comment's txBody is a standard a:CT_TextBody, but the OOXML spec only
    fixes the namespace URI, not which prefix a given producer binds it to
    (a default/unprefixed binding is also legal XML) - hardcoding "a:" would
    silently scrub nothing in a part that happens to bind it differently.
    Defaults to "a" (the prefix every real-world producer - PowerPoint,
    LibreOffice, python-pptx - actually uses) when no explicit binding is
    found in the part."""
    m = re.search(r'xmlns:([\w.-]+)=["\']' + re.escape(_DRAWINGML_NS) + r'["\']', xml_text)
    return m.group(1) if m else "a"


def _scrub_ooxml(path: str, surface_to_token: dict[str, str], style: str) -> int:
    tmp_path = path + ".commenttmp"
    total_changed = 0
    with zipfile.ZipFile(path, "r") as zin:
        names = zin.namelist()
        # Modern (2018-schema) comment/author parts aren't at a fixed path -
        # resolved via the relationship graph, same helper comment_scan uses
        # for scanning, so scan and scrub can never disagree about which
        # parts exist.
        modern_comment_parts = set(_modern_comment_parts(zin))
        modern_author_part = _modern_author_part(zin)
        author_parts = {n for n in ("ppt/commentAuthors.xml",) if n in names}
        if modern_author_part:
            author_parts.add(modern_author_part)

        replacements: dict[str, bytes] = {}
        for name in names:
            if name in author_parts:
                try:
                    text = zin.read(name).decode("utf-8")
                except UnicodeDecodeError:
                    continue
                new_text, changed = _scrub_attribute_values(text, _AUTHOR_ATTRS, surface_to_token, style)
                if changed:
                    replacements[name] = new_text.encode("utf-8")
                    total_changed += changed
                continue

            # Modern-comment-part membership (relationship-resolved, so
            # authoritative) is checked BEFORE the static path patterns -
            # a producer whose modern comment part happens to sit at a path
            # that also matches the legacy regex must still be scrubbed as
            # modern (sniffed DrawingML tag), not misidentified as legacy
            # (which would search for a "p:text" element that doesn't exist
            # there and silently scrub nothing).
            is_modern_comment = name in modern_comment_parts
            tags = None if is_modern_comment else next((t for pattern, t in _PART_RULES if pattern.match(name)), None)
            if tags is None and not is_modern_comment:
                continue
            try:
                text = zin.read(name).decode("utf-8")
            except UnicodeDecodeError:
                continue
            if is_modern_comment:
                # Sniff the ACTUAL prefix bound to DrawingML in this part
                # rather than assuming "a:" - see _drawingml_prefix.
                tags = [f"{_drawingml_prefix(text)}:t"]
            new_text, changed = _scrub_element_text(text, tags, surface_to_token, style)
            if changed:
                replacements[name] = new_text.encode("utf-8")
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
        for annot in page.annots() or []:
            content = (annot.info or {}).get("content", "")
            if not content:
                continue
            new_content = _substitute(content, surface_to_token, style)
            if new_content != content:
                annot.set_info(content=new_content)
                annot.update()
                changed += 1
    if changed:
        doc.saveIncr()
    doc.close()
    return changed


def scrub_comments(path: str, content_type: str, filename: str, surface_to_token: dict[str, str], style: str) -> int:
    """In-place. Returns the number of comment/tracked-change fragments rewritten."""
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
