"""Scrub descr/title/name alt-text attributes on cNvPr/docPr elements - the
write-side counterpart to alttext_scan.py. Operates on the ALREADY-RENDERED
masked file in place, same "render, then clean the rest" sequencing as
metadata/hyperlink/comment scrubbing.

`name` is included here for defense in depth even though alttext_scan's
extract_alt_text() doesn't feed it into detect()'s candidates by default (a
default shape name like "Picture 3" has no PII value, so scrubbing it is a
safe no-op) - this covers the rare producer that puts something meaningful
there instead, and verify's residual check (include_name=True) is what
actually proves this coverage works rather than just existing unverified.
"""

import re
import shutil
import zipfile
from xml.sax.saxutils import escape, unescape

from app.documents.alttext_scan import TAG_RE, target_parts
from app.masking.pattern import surface_pattern
from app.masking.style import replacement_for

_SCRUB_ATTR_RE = re.compile(r'\b(descr|title|name)=(["\'])((?:(?!\2).)*)\2')


def _substitute(text: str, surface_to_token: dict[str, str], style: str) -> str:
    out = text
    for surface in sorted(surface_to_token.keys(), key=len, reverse=True):
        token = surface_to_token[surface]
        out = re.sub(
            surface_pattern(surface), lambda m: replacement_for(m.group(0), token, style), out, flags=re.IGNORECASE
        )
    return out


def _scrub_part(xml_text: str, surface_to_token: dict[str, str], style: str) -> tuple[str, int]:
    changed = 0

    def _sub_tag(m: "re.Match") -> str:
        nonlocal changed
        tag_name, attrs, slash = m.group(1), m.group(2), m.group(3)

        def _sub_attr(am: "re.Match") -> str:
            nonlocal changed
            attr, quote, value = am.group(1), am.group(2), am.group(3)
            raw = unescape(value)
            new_raw = _substitute(raw, surface_to_token, style)
            if new_raw == raw:
                return am.group(0)
            changed += 1
            return f"{attr}={quote}{escape(new_raw)}{quote}"

        new_attrs = _SCRUB_ATTR_RE.sub(_sub_attr, attrs)
        return f"<{tag_name}{new_attrs}{slash}>"

    out = TAG_RE.sub(_sub_tag, xml_text)
    return out, changed


def scrub_alt_text(path: str, content_type: str, filename: str, surface_to_token: dict[str, str], style: str) -> int:
    """In-place. Returns the number of descr/title/name attributes rewritten."""
    if not surface_to_token:
        return 0
    tmp_path = path + ".alttmp"
    total_changed = 0
    try:
        with zipfile.ZipFile(path, "r") as zin:
            parts = target_parts(zin, content_type, filename)
            replacements: dict[str, bytes] = {}
            for name in parts:
                try:
                    text = zin.read(name).decode("utf-8")
                except UnicodeDecodeError:
                    continue
                new_text, changed = _scrub_part(text, surface_to_token, style)
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
    except Exception:
        return 0
    return total_changed
