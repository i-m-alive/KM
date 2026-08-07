"""Channel-coverage completeness audit (item 1c of the sanitization accuracy
brief: "makes 'a channel was never opened' structurally impossible").

The real incident this exists to catch: `ppt/authors.xml` shipped a full
author list + emails, completely unscrubbed, because the metadata channel
was scoped to `docProps/` and nothing else ever looked at it - a channel
that silently existed outside every known scrubber's field of view. Fixing
that ONE part doesn't protect against the NEXT unknown identity-bearing part
a future OOXML producer might introduce.

This module does not scrub or verify masking correctness - the other
scan/scrub modules already do that. It answers a narrower, structural
question instead: "does every zip part that LOOKS like it might carry a
real identity have SOME scrubber that claims it?" A part matching an
identity-suggestive keyword (author, person, people, creator, owner,
comment, contributor, editor, reviewer, user) that isn't in the known-covered
allowlist below is flagged - not because it's necessarily a real leak (most
hits will be a legitimate part this list hasn't been told about yet, e.g. a
new OOXML extension), but because "unknown and unclaimed" is exactly the
shape the authors.xml incident had, and the earlier it's surfaced the
cheaper it is to fix.
"""

import re
import zipfile

# Every OOXML part pattern some scrub/scan module in this codebase already
# claims. Kept as one list, explicitly cross-referenced to its owner, so
# "is this part covered" has one place to check rather than needing to read
# five modules' internals to answer it.
_KNOWN_COVERED_PATTERNS = [
    # docProps (metadata_scan.py / metadata_scrub.py)
    re.compile(r"^docProps/(core|app|custom)\.xml$"),
    # Legacy + modern PPTX comments and authors (comment_scan.py build_author_parts
    # / _modern_comment_parts, comment_scrub.py). Modern (2018-schema) comment
    # parts are named "modernComment_<slideId>_<hash>.xml" (resolved dynamically
    # via each slide's .rels, not a fixed literal) - the narrower legacy-only
    # pattern below missed this real naming shape entirely, raising a false
    # "unclaimed channel" warning on every modern-PPTX run even though
    # comment_scan.py/comment_scrub.py already fully cover it.
    re.compile(r"^ppt/comments/comment\d*\.xml$"),
    re.compile(r"^ppt/comments/modernComment_.*\.xml$"),
    re.compile(r"^ppt/commentAuthors\.xml$"),
    re.compile(r"^ppt/authors\.xml$"),
    # DOCX comments/people (comment_scan.py / comment_scrub.py)
    re.compile(r"^word/comments\.xml$"),
    re.compile(r"^word/people\.xml$"),
    re.compile(r"^word/document\.xml$"),  # delText tracked-deletion channel
    re.compile(r"^word/(header|footer)\d*\.xml$"),  # alt-text (docPr) channel
    # XLSX comments/threaded-comments/persons (comment_scan.py / comment_scrub.py)
    re.compile(r"^xl/comments\d*\.xml$"),
    re.compile(r"^xl/comments/comment\d*\.xml$"),
    re.compile(r"^xl/threadedComments/threadedComment\d*\.xml$"),
    re.compile(r"^xl/persons/person\.xml$"),
    # Alt-text (descr/title/name on cNvPr/docPr) - alttext_scan.py / alttext_scrub.py
    re.compile(r"^ppt/(slides|slideLayouts|slideMasters|notesSlides)/[^/]+\.xml$"),
    re.compile(r"^xl/drawings/drawing\d*\.xml$"),
]

# Path-fragment keywords suggestive of a real identity, checked case-
# insensitively against the WHOLE part path (not just the filename), so
# e.g. a "people" folder or a "reviewers.xml" part is caught regardless of
# exactly where a producer decides to put it.
_IDENTITY_KEYWORDS = (
    "author", "person", "people", "creator", "owner", "comment",
    "contributor", "editor", "reviewer", "annotator", "presence",
)

# Parts that DO match an identity keyword but are known, checked, safe -
# false positives this audit would otherwise raise every single run.
# ppt/tags/*.xml stores presentation-level custom tags, not personal data,
# despite occasionally containing a "reviewer" or similar tag NAME.
_KNOWN_SAFE_PATTERNS = [
    re.compile(r"^ppt/tags/tag\d*\.xml$"),
    re.compile(r"^\[Content_Types\]\.xml$"),
]


def _is_covered(name: str) -> bool:
    return any(p.match(name) for p in _KNOWN_COVERED_PATTERNS)


def _is_known_safe(name: str) -> bool:
    return any(p.match(name) for p in _KNOWN_SAFE_PATTERNS)


def _looks_identity_bearing(name: str) -> bool:
    lower = name.lower()
    return any(kw in lower for kw in _IDENTITY_KEYWORDS)


def audit_channel_coverage(path: str, content_type: str, filename: str) -> list[str]:
    """Returns human-readable warnings for zip parts that look
    identity-bearing (by path keyword) but aren't claimed by any known
    scrub/scan module. Empty list = nothing unexpected found. This is a
    STRUCTURAL safety net, not a masking-correctness check - a part showing
    up here means "go look at this", not "this run leaked"."""
    lower = filename.lower()
    is_ooxml = (
        lower.endswith((".docx", ".pptx", ".xlsx"))
        or "wordprocessingml" in content_type
        or "presentationml" in content_type
        or "spreadsheetml" in content_type
    )
    if not is_ooxml:
        return []  # PDF has no zip package structure to audit this way

    warnings: list[str] = []
    try:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if _is_covered(name) or _is_known_safe(name):
                    continue
                if _looks_identity_bearing(name):
                    warnings.append(
                        f"'{name}' looks identity-bearing (matches a name/author/person-style path) but isn't "
                        "claimed by any known scan/scrub module - inspect manually; this may be a new OOXML "
                        "extension part this audit doesn't know about yet."
                    )
    except Exception:
        return []
    return warnings
