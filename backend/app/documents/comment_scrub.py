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
second shape entirely - stored as XML ATTRIBUTES (name=, initials=, ...),
not element text - so those go through _clear_attribute_values instead
(unconditional emptying, not surface_to_token substitution - see below),
same text-preserving, no-reserialization approach applied to attribute
values.

PowerPoint modern/cloud (threaded) comments are scrubbed alongside legacy
ones - see comment_scan's _modern_comment_parts/_modern_author_part, reused
here since the modern Comment/Author parts' on-disk paths aren't fixed by
the OOXML spec (only their relationship type is) and must be resolved the
same way for scrub as for scan, or the two could silently disagree about
which parts exist. The modern comment part's txBody is a standard
a:CT_TextBody (ISO/IEC29500-1 A.4.1) - the same a:t run-text tag used
everywhere else in DrawingML, not a bespoke element.

Excel threaded comments are scrubbed the same way - see comment_scan's
_xlsx_threaded_comment_parts/build_author_parts, reused here so scrub can
never disagree with scan about which parts exist. Threaded comment text
lives in a plain <text> element (no namespace prefix in every real producer
observed); person display names live in a displayName= ATTRIBUTE, a
different attribute name than PPTX/DOCX's name=/initials= but the same
attribute-not-element-text shape. Legacy xlsx <author> names are their own
element TEXT inside the SAME comments part as the comment body - previously
unscrubbed, since openpyxl's comment API only ever touches body text.

DOCX comment authors get the same treatment on two fronts: legacy
w:author/w:initials ATTRIBUTES on <w:comment> itself (word/comments.xml -
previously unscrubbed even though that part's BODY text was already being
handled) and the modern per-GUID people registry (word/people.xml).
word/comments.xml needs BOTH the attribute pass and the element-text pass
applied to the SAME part - the scrub loop below chains them rather than
picking one exclusively.

PowerPoint's modern author part ALSO carries userId, which embeds the
actual email (e.g. "S::ankit.bajpai@zs.com::<GUID>") - scrubbing only
name/initials left the real leak (the email) completely untouched.

Author-identity attributes (name/initials/userId/author/displayName) are
cleared UNCONDITIONALLY - not run through _scrub_attribute_values'
surface_to_token substitution. Real observed failure: authors.xml was
completely untouched on a produced file because nothing in detect() had
ever looked at authors.xml/people.xml/person.xml, so the names/emails there
were never in surface_to_token to begin with - there was nothing to
substitute regardless of how correct the scrub wiring was. An author byline
is ALWAYS a real identity by construction (same reasoning as
metadata_scrub.py's dc:creator/cp:lastModifiedBy/cp:manager), so gating it
on whether that specific name/email happened to get detected elsewhere is
the same fragile pattern that caused the leak - clearing it outright doesn't
depend on detection succeeding anywhere.
"""

import re
import shutil
import zipfile
from xml.sax.saxutils import escape, unescape

from app.documents.comment_scan import (
    _modern_comment_parts,
    _xlsx_threaded_comment_parts,
    build_author_parts,
)
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


def _clear_attribute_values(xml_text: str, attr_names: tuple[str, ...]) -> tuple[str, int]:
    """Unconditionally empty the given ATTRIBUTE values, anywhere they appear
    - no surface_to_token lookup at all. For author-identity attributes
    (name/initials/userId/author/displayName): these are always a real
    person's identity by construction, so - same reasoning as
    metadata_scrub.py's identity-field clearing - correctness can't depend on
    that specific name/email having been detected as a client entity
    somewhere else first."""
    changed = 0

    def _sub(m: re.Match) -> str:
        nonlocal changed
        attr, quote, value = m.group(1), m.group(2), m.group(3)
        if not value:
            return m.group(0)
        changed += 1
        return f"{attr}={quote}{quote}"

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
    # openpyxl (and newer producers) write xl/comments/comment1.xml. "author"
    # is the legacy <authors><author>Name</author>...</authors> list - its
    # own element text, separate from the comment body ("t") and previously
    # unscrubbed entirely (openpyxl's own comment API only ever touches body
    # text, never this list).
    (re.compile(r"^xl/comments\d*\.xml$"), ["t", "author"]),
    (re.compile(r"^xl/comments/comment\d*\.xml$"), ["t", "author"]),
]

# Author/person-identity attribute tuples per part shape now live in
# comment_scan.build_author_parts() - the single source of truth for both
# this module's clearing and comment_scan's residual check.

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
        # Modern (2018-schema) PPTX comment parts and Excel threaded-comment
        # parts aren't at a spec-fixed path - resolved via the relationship
        # graph (with a static-path fallback), same helpers comment_scan uses
        # for scanning, so scan and scrub can never disagree about which
        # parts exist.
        modern_comment_parts = set(_modern_comment_parts(zin))
        xlsx_threaded_parts = set(_xlsx_threaded_comment_parts(zin))

        # Author/person-identity parts, and which attributes carry PII on
        # each - built by the SAME shared function comment_scan's
        # unconditional residual check uses, so scan and scrub can never
        # disagree here either. word/comments.xml appears here AND matches
        # _PART_RULES below - it needs BOTH the attribute pass (author
        # identity) and the element-text pass (comment body) applied to the
        # SAME part, not one exclusive branch, which is why the loop below
        # chains rather than dispatches on either/or.
        author_parts = build_author_parts(zin)

        replacements: dict[str, bytes] = {}
        for name in names:
            text: str | None = None
            changed_here = 0

            attrs_for_part = author_parts.get(name)
            if attrs_for_part:
                try:
                    text = zin.read(name).decode("utf-8")
                except UnicodeDecodeError:
                    continue
                text, c = _clear_attribute_values(text, attrs_for_part)
                changed_here += c

            # Modern/threaded-comment-part membership (relationship-resolved,
            # so authoritative) is checked BEFORE the static path patterns -
            # a producer whose modern comment part happens to sit at a path
            # that also matches a legacy regex must still be scrubbed as
            # modern (sniffed DrawingML tag for PPTX, plain <text> for xlsx),
            # not misidentified as legacy (which would search for an element
            # that doesn't exist there and silently scrub nothing).
            is_modern_comment = name in modern_comment_parts
            is_xlsx_threaded = name in xlsx_threaded_parts
            if is_modern_comment or is_xlsx_threaded:
                tags = None  # resolved below, after reading the part's text
            else:
                tags = next((t for pattern, t in _PART_RULES if pattern.match(name)), None)

            if tags is not None or is_modern_comment or is_xlsx_threaded:
                if text is None:
                    try:
                        text = zin.read(name).decode("utf-8")
                    except UnicodeDecodeError:
                        text = None
                if text is not None:
                    if is_modern_comment:
                        # Sniff the ACTUAL prefix bound to DrawingML in this
                        # part rather than assuming "a:" - see _drawingml_prefix.
                        tags = [f"{_drawingml_prefix(text)}:t"]
                    elif is_xlsx_threaded:
                        # Threaded-comment text is a plain, unprefixed <text>
                        # element in every producer observed - no
                        # DrawingML-style prefix ambiguity here.
                        tags = ["text"]
                    text, c = _scrub_element_text(text, tags, surface_to_token, style)
                    changed_here += c

            if text is not None and changed_here:
                replacements[name] = text.encode("utf-8")
                total_changed += changed_here
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
            info = annot.info or {}
            # The annotation Title field is the author's real identity by
            # construction (same shape as PPTX/DOCX comment author name= -
            # see comment_scan._scan_pdf) - cleared UNCONDITIONALLY, not
            # gated on surface_to_token containing that specific name.
            title = (info.get("title") or "").strip()
            content = info.get("content", "")
            new_content = _substitute(content, surface_to_token, style) if content else content
            title_changed = bool(title)
            content_changed = bool(content) and new_content != content
            if not (title_changed or content_changed):
                continue
            kwargs = {}
            if title_changed:
                # PyMuPDF's set_info does `if title: ...` internally, so
                # title="" is silently ignored (verified empirically - the
                # old value survives untouched). A single space clears the
                # readable identity while still being non-empty enough for
                # set_info to actually apply it; _scan_pdf's .strip() check
                # correctly treats whitespace-only as clean.
                kwargs["title"] = " "
            if content_changed:
                kwargs["content"] = new_content
            annot.set_info(**kwargs)
            annot.update()
            changed += 1
    if changed:
        doc.saveIncr()
    doc.close()
    return changed


def scrub_comments(path: str, content_type: str, filename: str, surface_to_token: dict[str, str], style: str) -> int:
    """In-place. Returns the number of comment/tracked-change fragments rewritten.

    Deliberately does NOT early-return when surface_to_token is empty -
    author-identity clearing is unconditional hygiene, not tied to whether
    anything was actually masked in this run."""
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
