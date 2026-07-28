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
relationship graph, UNIONED with a static fallback on the conventional
ppt/authors.xml path (real observed failure: relationship resolution alone
found nothing on a real produced file, silently leaking 7 author names +
emails). Comment AUTHOR display names (legacy ppt/commentAuthors.xml and its
modern equivalent) are themselves PII and are checked too - the modern
author part's userId ALSO embeds the actual email
(e.g. "S::ankit.bajpai@zs.com::<GUID>"), checked as its own attribute since
name/initials alone miss it entirely.

DOCX comment authors are checked two ways: the legacy w:author/w:initials
ATTRIBUTES on <w:comment> itself in word/comments.xml (previously missed -
only the comment BODY text was ever scanned), and the modern per-GUID
people registry (word/people.xml, relationship-resolved with the same
static-path fallback reasoning as PPTX/xlsx).

Excel threaded (modern/cloud) comments are checked alongside legacy xlsx
cell comments (see _xlsx_threaded_comment_parts / _xlsx_person_part) - same
relationship-graph-first reasoning as PPTX's modern comments, UNIONED with a
glob on the conventional xl/threadedComments/*.xml path as a defensive
backstop (every real Excel file with modern comments observed uses this
exact path, so a slightly-off relationship-type string still doesn't
silently miss them - the same belt-and-suspenders approach already used for
orphaned PPTX media). Person display names (xl/persons/person.xml) are
themselves PII, same reasoning as PPTX comment authors, and are checked too.

Author/person-identity attributes (name/initials/userId/author/displayName)
get an UNCONDITIONAL residual check via build_author_parts() +
_residual_identity_attrs() - independent of `surfaces` - mirroring
metadata_scan's identity-field check. Real observed failure: authors.xml
leaked 7 names + emails on a file whose body was otherwise clean, because
nothing had ever detected those specific names/emails as client entities -
gating this check on `surfaces` would have exactly the same blind spot as
the scrub side did before this was made unconditional. build_author_parts()
is shared with comment_scrub.py so scan and scrub can never disagree about
which parts exist or which attributes matter on each.
"""

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

from app.masking.pattern import surface_pattern

_MODERN_COMMENTS_RELTYPE = "http://schemas.microsoft.com/office/2018/10/relationships/comments"
_MODERN_AUTHORS_RELTYPE = "http://schemas.microsoft.com/office/2018/10/relationships/authors"
_XLSX_THREADED_COMMENT_RELTYPE = "http://schemas.microsoft.com/office/2017/06/relationships/threadedComment"
_XLSX_PERSON_RELTYPE = "http://schemas.microsoft.com/office/2017/10/relationships/person"


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


_DEFAULT_AUTHOR_ATTRS = ("name", "initials")


def _author_attr_text(z: zipfile.ZipFile, partname: str, attrs: tuple[str, ...] = _DEFAULT_AUTHOR_ATTRS) -> str:
    """Author display names/initials/userId in a commentAuthors-shaped part -
    stored as XML ATTRIBUTES, not element text, on both the legacy (p:cmAuthor)
    and modern author element - so this reads attribute values instead of
    _part_text's element-text walk. Generic over the child element's exact
    tag name (deliberately not hardcoded), since only the relationship
    type/content type of the modern Author part are spec-guaranteed, not its
    internal element names. `attrs` varies by part shape: legacy/DOCX use
    name+initials (or author+initials); PPTX's modern author part ALSO
    carries userId, which embeds the actual email
    (e.g. "S::ankit.bajpai@zs.com::<GUID>") - the real observed leak this
    closes, missed entirely when this only checked name/initials.

    Matched by LOCAL attribute name, not exact key: DOCX's w:author/
    w:initials on <w:comment> are namespace-PREFIXED attributes - ET parses
    these into Clark notation ("{wordprocessingml-ns}author"), so a bare
    el.get("author") silently returns None even though the attribute is
    right there. PPTX/xlsx's name=/initials=/userId=/displayName= are
    unprefixed, where el.get(...) already works - matching on local name
    covers both shapes with one implementation instead of two."""
    if partname not in z.namelist():
        return ""
    try:
        root = ET.fromstring(z.read(partname))
    except ET.ParseError:
        return ""
    values = []
    for el in root.iter():
        for key, v in el.attrib.items():
            local = key.split("}")[-1] if "}" in key else key
            if local in attrs and v and v.strip():
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
    relationship from the Presentation part - UNIONED with a static fallback
    on the conventional ppt/authors.xml path. A real produced file leaked its
    entire author list (7 names + emails) because relationship resolution
    alone found nothing - PowerPoint's actual authors.xml either uses a
    relationship-type string this constant doesn't match, or something else
    about resolution didn't fire; either way, a wrong/unverifiable
    relationship-type guess must not cause a silent miss on a path every
    real PowerPoint file with modern comments actually uses, same
    belt-and-suspenders reasoning as the xlsx threaded-comment/person parts."""
    targets = _rels_targets(z, "ppt/_rels/presentation.xml.rels", _MODERN_AUTHORS_RELTYPE, "ppt")
    if targets:
        return targets[0]
    return "ppt/authors.xml" if "ppt/authors.xml" in z.namelist() else None


_DOCX_PEOPLE_RELTYPE = "http://schemas.microsoft.com/office/2011/relationships/people"


def _docx_people_part(z: zipfile.ZipFile) -> str | None:
    """DOCX's analogue of PPTX's modern author part / xlsx's person part -
    author display names behind modern/threaded comments, keyed by GUID.
    Same relationship-resolution-first, static-path-fallback reasoning;
    the exact relationship-type string is a best-effort guess (unverifiable
    without a real sample file), so the conventional word/people.xml path is
    what actually carries this check in practice."""
    targets = _rels_targets(z, "word/_rels/document.xml.rels", _DOCX_PEOPLE_RELTYPE, "word")
    if targets:
        return targets[0]
    return "word/people.xml" if "word/people.xml" in z.namelist() else None


def build_author_parts(z: zipfile.ZipFile) -> dict[str, tuple[str, ...]]:
    """Maps every author/person-identity part actually present in this
    package to the specific attribute names that carry PII there. Shared by
    this module's unconditional residual check and comment_scrub's
    unconditional clearing, so scan and scrub can never disagree about which
    parts exist or which attributes matter on each - the same discipline
    already applied to modern-comment-part resolution."""
    names = z.namelist()
    author_parts: dict[str, tuple[str, ...]] = {}
    if "ppt/commentAuthors.xml" in names:
        author_parts["ppt/commentAuthors.xml"] = ("name", "initials")
    modern_author_part = _modern_author_part(z)
    if modern_author_part:
        author_parts[modern_author_part] = ("name", "initials", "userId")
    xlsx_person_part = _xlsx_person_part(z)
    if xlsx_person_part:
        author_parts[xlsx_person_part] = ("displayName",)
    if "word/comments.xml" in names:
        author_parts["word/comments.xml"] = ("author", "initials")
    docx_people_part = _docx_people_part(z)
    if docx_people_part:
        author_parts[docx_people_part] = ("author", "userId")
    return author_parts


def _residual_identity_attrs(z: zipfile.ZipFile, partname: str, attrs: tuple[str, ...]) -> list[str]:
    """Unconditional: any of `attrs` still non-empty on any element in this
    part is itself the leak, regardless of whether its value matches
    anything in `surfaces` - these attributes are always a real identity by
    construction (see module docstring)."""
    if partname not in z.namelist():
        return []
    try:
        root = ET.fromstring(z.read(partname))
    except ET.ParseError:
        return []
    hits = []
    for el in root.iter():
        for key, v in el.attrib.items():
            local = key.split("}")[-1] if "}" in key else key
            if local in attrs and v and v.strip():
                hits.append(f"{partname}: identity attribute '{local}' still populated: '{v.strip()}'")
    return hits


def _xlsx_threaded_comment_parts(z: zipfile.ZipFile) -> list[str]:
    """Modern (threaded) Excel comment parts. Resolved primarily via each
    worksheet's OWN relationships (same reasoning as PPTX's modern comments -
    the relationship type is what the spec actually fixes, not the path),
    UNIONED with a glob on the conventional xl/threadedComments/*.xml path -
    every real Excel file with modern comments observed uses exactly this
    path, so a mismatched/unrecognized relationship type string still
    doesn't silently miss them."""
    parts = set()
    for name in z.namelist():
        if re.match(r"^xl/worksheets/sheet\d+\.xml$", name):
            rels_name = f"xl/worksheets/_rels/{posixpath.basename(name)}.rels"
            parts.update(_rels_targets(z, rels_name, _XLSX_THREADED_COMMENT_RELTYPE, "xl/worksheets"))
    parts.update(name for name in z.namelist() if re.match(r"^xl/threadedComments/threadedComment\d*\.xml$", name))
    return sorted(parts)


def _xlsx_person_part(z: zipfile.ZipFile) -> str | None:
    """At most one Person part per workbook (the display-name registry behind
    every threaded comment's personId) - relationship-resolved first, same
    conventional-path fallback reasoning as _xlsx_threaded_comment_parts."""
    targets = _rels_targets(z, "xl/_rels/workbook.xml.rels", _XLSX_PERSON_RELTYPE, "xl")
    if targets:
        return targets[0]
    return "xl/persons/person.xml" if "xl/persons/person.xml" in z.namelist() else None


def _display_name_attr_text(z: zipfile.ZipFile, partname: str) -> str:
    """Person display names - stored as a displayName= XML ATTRIBUTE on each
    <person> element, not element text, same shape as PPTX comment authors'
    name=/initials= attributes but a different attribute name. Delegates to
    _author_attr_text (local-attribute-name matched) rather than a bare
    el.get(...), the same fix that closed a real miss on DOCX's
    namespace-prefixed w:author - cheap insurance against the same bug here
    even though today's real-world xlsx producers use displayName unprefixed."""
    return _author_attr_text(z, partname, attrs=("displayName",))


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


def _unconditional_identity_hits(z: zipfile.ZipFile) -> list[str]:
    """Author/person identity-attribute residuals, independent of `surfaces`
    - see build_author_parts/_residual_identity_attrs and module docstring."""
    hits: list[str] = []
    for partname, attrs in build_author_parts(z).items():
        hits.extend(_residual_identity_attrs(z, partname, attrs))
    return hits


def _scan_docx(path: str, surfaces: list[str]) -> list[str]:
    hits: list[str] = []
    try:
        with zipfile.ZipFile(path) as z:
            hits.extend(_unconditional_identity_hits(z))
            hits.extend(_find_in_text(_part_text(z, "word/comments.xml"), surfaces, "comment"))
            # w:author/w:initials on <w:comment> itself - attributes, not
            # element text, so _part_text's element-text walk never saw
            # these; a comment author's real name survived untouched even
            # though the comment BODY text was already being scrubbed.
            hits.extend(_find_in_text(
                _author_attr_text(z, "word/comments.xml", attrs=("author", "initials")), surfaces, "comment author",
            ))
            people_part = _docx_people_part(z)
            if people_part:
                hits.extend(_find_in_text(
                    _author_attr_text(z, people_part, attrs=("author", "userId")), surfaces, "comment author",
                ))
            for partname in ("word/document.xml", "word/comments.xml"):
                hits.extend(_find_in_text(_deltext_only(z, partname), surfaces, "tracked deletion"))
    except Exception:
        pass
    return hits


def _scan_pptx(path: str, surfaces: list[str]) -> list[str]:
    hits: list[str] = []
    try:
        with zipfile.ZipFile(path) as z:
            hits.extend(_unconditional_identity_hits(z))
            for name in z.namelist():
                if re.match(r"ppt/comments/comment\d*\.xml$", name):
                    hits.extend(_find_in_text(_part_text(z, name), surfaces, "comment"))
            if "ppt/commentAuthors.xml" in z.namelist():
                hits.extend(_find_in_text(_author_attr_text(z, "ppt/commentAuthors.xml"), surfaces, "comment author"))
            for name in _modern_comment_parts(z):
                hits.extend(_find_in_text(_part_text(z, name), surfaces, "comment"))
            author_part = _modern_author_part(z)
            if author_part:
                # userId embeds the actual email (e.g.
                # "S::ankit.bajpai@zs.com::<GUID>") - the real leak: name/
                # initials alone missed the email entirely.
                hits.extend(_find_in_text(
                    _author_attr_text(z, author_part, attrs=("name", "initials", "userId")), surfaces, "comment author",
                ))
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
    try:
        with zipfile.ZipFile(path) as z:
            hits.extend(_unconditional_identity_hits(z))
            # Legacy xlsx comments keep an <authors><author>Name</author>...
            # list as its own element TEXT, separate from the comment body -
            # openpyxl's cell.comment.text above only ever sees the body.
            for name in z.namelist():
                if re.match(r"^xl/comments\d*\.xml$", name) or re.match(r"^xl/comments/comment\d*\.xml$", name):
                    hits.extend(_find_in_text(_part_text(z, name), surfaces, "comment"))
            for name in _xlsx_threaded_comment_parts(z):
                hits.extend(_find_in_text(_part_text(z, name), surfaces, "threaded comment"))
            person_part = _xlsx_person_part(z)
            if person_part:
                hits.extend(_find_in_text(_display_name_attr_text(z, person_part), surfaces, "threaded comment author"))
    except Exception:
        pass
    return hits


def _scan_pdf(path: str, surfaces: list[str]) -> list[str]:
    """PDF annotations carry TWO separate PII-relevant fields: `content`
    (the visible comment text - checked against `surfaces`, same as every
    other channel) and `title` (the standard PDF markup-annotation Title
    field, which every real PDF viewer/producer uses for the ANNOTATION
    AUTHOR's display name - the same "always a real identity by
    construction" shape as author name= on PPTX/DOCX comments). `title` is
    checked UNCONDITIONALLY, independent of `surfaces`, same reasoning as
    build_author_parts()/_residual_identity_attrs() for the OOXML formats."""
    import fitz

    hits: list[str] = []
    doc = fitz.open(path)
    for page_number, page in enumerate(doc):
        for annot in page.annots() or []:
            info = annot.info or {}
            title = (info.get("title") or "").strip()
            if title:
                hits.append(f"annotation on page {page_number + 1}: identity attribute 'title' still populated: '{title}'")
            content = info.get("content", "")
            if content:
                hits.extend(_find_in_text(content, surfaces, f"annotation on page {page_number + 1}"))
    doc.close()
    return hits


def find_residual_comments(path: str, content_type: str, filename: str, surfaces: list[str]) -> list[str]:
    """Deliberately does NOT early-return when `surfaces` is empty - the
    author/person identity-attribute check runs unconditionally (see module
    docstring), independent of any specific detected surface."""
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
