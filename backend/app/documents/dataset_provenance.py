"""Dataset provenance check (Phase 2) - advisory, at upload time.

Flags a newly-uploaded file that already traces back to a client account
Sanitization has previously linked identity for, via the SAME masking
dictionary + source registry every other Phase 1/2 detection reuses: if the
new file's text matches an APPROVED entity that's already linked to a real
`client_account_id`, this file is not a blank slate - it may be raw client
material (or a copy/export of a previously-sanitized document) that
shouldn't be reused for training/testing/analysis without going through
Sanitization first.

Deliberately does not block the upload (Sanitization itself, not the upload
endpoint, is where a real leak gets caught and stopped) - this is an early,
advisory signal so it's visible before the review queue, not instead of it.
"""

from sqlalchemy.orm import Session

from app.documents.extract import extract_chunks
from app.masking import dictionary


def check(db: Session, stored_path: str, content_type: str, filename: str) -> str | None:
    """Returns a human-readable warning if this document's text matches an
    already-tracked, account-linked entity, else None. Best-effort: any
    extraction failure here degrades to "no warning" rather than blocking
    the upload response - the same call already validated extractability
    before this runs, so a failure here is a lower-stakes second read, not
    the thing standing between the user and a working upload."""
    try:
        chunks = extract_chunks(stored_path, content_type, filename)
        full_text = "\n".join(c.text for c in chunks)
    except Exception:
        return None
    if not full_text.strip():
        return None

    linked_accounts: dict[str, str] = {}
    for entity, matched_surface in dictionary.find_in_text(db, full_text):
        if entity.client_account_id is None:
            continue
        account = entity.client_account
        name = account.name if account is not None else str(entity.client_account_id)
        linked_accounts[name] = matched_surface

    if not linked_accounts:
        return None
    pairs = ", ".join(f'{name} (matched "{surface}")' for name, surface in linked_accounts.items())
    return (
        f"This document appears to reference {len(linked_accounts)} already-tracked client account"
        f"{'s' if len(linked_accounts) != 1 else ''}: {pairs}. If this is raw client material or a copy of "
        "previously-sanitized content, run it through Sanitization before reusing it for training, testing, "
        "or analysis."
    )
