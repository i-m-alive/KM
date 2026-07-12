"""Post-render verification: re-read the just-rendered masked file and confirm
none of the original surface strings survive in it. This is what actually
answers "is Sanitization working" - rather than trusting the render step
succeeded, we check its output the same way we'd check any other system.
"""

import re

from app.documents.extract import extract_chunks
from app.masking.pattern import surface_pattern


def find_residual_surfaces(masked_path: str, content_type: str, filename: str, surfaces: list[str]) -> list[str]:
    """Returns the subset of `surfaces` that still appear (case-insensitive)
    somewhere in the rendered masked file. Empty list = clean."""
    if not surfaces:
        return []
    try:
        chunks = extract_chunks(masked_path, content_type, filename)
    except Exception:
        # Can't verify (e.g. unsupported/corrupt render) - treat as unverifiable,
        # not as "clean". Caller decides how to surface this.
        return ["<verification could not read the rendered file>"]

    full_text = "\n".join(c.text for c in chunks)
    residual = []
    for surface in surfaces:
        # Word-boundary match, same as masking itself - a plain substring
        # search would flag e.g. "sure" surviving inside "exposure" as a
        # leak, when that was never a real occurrence of the masked surface.
        if re.search(surface_pattern(surface), full_text, flags=re.IGNORECASE):
            residual.append(surface)
    return residual
