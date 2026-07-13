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
leaving every byte of markup untouched.

Same deliberate schema gaps as comment_scan: PowerPoint modern/cloud
comments and Excel threaded comments are out of scope; legacy comments only.
"""

import re
import shutil
import zipfile
from xml.sax.saxutils import escape, unescape

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


def _scrub_ooxml(path: str, surface_to_token: dict[str, str], style: str) -> int:
    tmp_path = path + ".commenttmp"
    total_changed = 0
    with zipfile.ZipFile(path, "r") as zin:
        replacements: dict[str, bytes] = {}
        for name in zin.namelist():
            tags = next((t for pattern, t in _PART_RULES if pattern.match(name)), None)
            if tags is None:
                continue
            try:
                text = zin.read(name).decode("utf-8")
            except UnicodeDecodeError:
                continue
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
