"""Post-render verification: re-read the just-rendered masked file and confirm
none of the original surface strings survive in it. This is what actually
answers "is Sanitization working" - rather than trusting the render step
succeeded, we check its output the same way we'd check any other system.

Covers body run text AND image alt-text (descr/title/name on cNvPr/docPr
elements) as one "text" channel - alt-text sits in a seam between this
channel (body runs) and the image channel (image_scan.py, pixels only), so
a name that survives ONLY in alt-text needs to be caught here, not treated
as a separate, easy-to-forget-about channel.
"""

import re

from app.documents.alttext_scan import extract_alt_text
from app.documents.extract import extract_chunks
from app.masking.pattern import surface_pattern


def find_residual_surfaces(masked_path: str, content_type: str, filename: str, surfaces: list[str]) -> list[str]:
    """Returns the subset of `surfaces` that still appear (case-insensitive)
    somewhere in the rendered masked file - body text or image alt-text.
    Empty list = clean."""
    if not surfaces:
        return []
    try:
        chunks = extract_chunks(masked_path, content_type, filename)
    except Exception:
        # Can't verify (e.g. unsupported/corrupt render) - treat as unverifiable,
        # not as "clean". Caller decides how to surface this.
        return ["<verification could not read the rendered file>"]

    # include_name=True (unlike detect()'s candidate feed) so this actually
    # proves the scrub side's defense-in-depth name= coverage works, not just
    # that it exists unverified.
    alt_texts = extract_alt_text(masked_path, content_type, filename, include_name=True)
    full_text = "\n".join(c.text for c in chunks) + ("\n" + "\n".join(alt_texts) if alt_texts else "")
    residual = []
    for surface in surfaces:
        # Word-boundary match, same as masking itself - a plain substring
        # search would flag e.g. "sure" surviving inside "exposure" as a
        # leak, when that was never a real occurrence of the masked surface.
        if re.search(surface_pattern(surface), full_text, flags=re.IGNORECASE):
            residual.append(surface)
    return residual
