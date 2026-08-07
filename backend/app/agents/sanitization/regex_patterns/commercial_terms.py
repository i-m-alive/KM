"""Deterministic, free candidate detection for Commercial & Financial Terms
and client-stakeholder job titles (Phase 3). Same role as infra_credential.py
- zero-cost candidates fed to the LLM Detector, never applied directly.

Deliberately loose on purpose, unlike infra_credential.py's CREDENTIAL
patterns: COMMERCIAL_TERM and CLIENT_PERSON_TITLE both default to FLAG, not
mask (see entity_actions.py) - a reviewer decides what to do with every
candidate, so a false-positive candidate here costs a reviewer one extra
row to glance at and dismiss, not an unwanted mandatory redaction. A currency
amount near "trial balance" isn't really a commercial term; one near
"contract value" is - that contextual call is the LLM Detector's job, not
this pre-pass's.
"""

import re

# ---- COMMERCIAL_TERM ----
# \b before the alphabetic currency CODES only (USD/EUR/...) - \b right
# before a symbol like $/€/£ never matches when preceded by whitespace: \b
# is a transition between a \w and a non-\w character, and a symbol
# preceded by a space is non-word-to-non-word on both sides, so there is no
# transition for \b to find. Wrapping the whole alternation in one outer \b
# silently made this regex match NOTHING for every symbol-prefixed amount
# ("$2.5M") while still working for the letter-coded ones ("USD 500,000") -
# exactly the kind of gap that's invisible until you plant a real fixture.
_CURRENCY_RE = re.compile(
    r"(?:\b(?:USD|EUR|GBP|INR)\b|US\$|\$|€|£)\s?\d[\d,]*(?:\.\d+)?\s?"
    r"(?:[MmKkBb]\b|million|billion|thousand|lakh|crore)?"
)
_PAYMENT_TERM_KEYWORDS_RE = re.compile(
    r"\b(?:net\s?-?\s?(?:30|60|90)|penalty clause|SLA credit|rate card|contract value|deal size|"
    r"payment terms?|not[- ]to[- ]exceed|\bNTE\b)\b",
    re.IGNORECASE,
)

# ---- CLIENT_PERSON_TITLE ----
# Multi-word / distinctive titles only - a bare "Manager" or "Director" alone
# is too generic/noisy a candidate; requiring the "of X" continuation or a
# well-known compound title keeps this a useful signal rather than flooding
# the proposal with common nouns.
#
# The "of X" continuation matches ONLY capitalized words (each one anchored
# on its own [A-Z][A-Za-z]*), not a loose [\w&,/ ]{2,40} character class -
# that class allowed spaces AND commas, so it happily ran on past the actual
# department name, across a comma, into the next clause of the sentence
# ("Vice President of Operations, who reports to the Chief" - swallowing
# "who reports to the Chief" as if it were part of the title) before an
# arbitrary 40-char cap cut it off mid-word. Ordinary prose after a title
# starts with a lowercase word ("who", "and", "reports"...) or punctuation,
# which this tighter pattern simply won't match into.
_DEPT = r"of\s+[A-Z][A-Za-z]*(?:\s+(?:and\s+|&\s*)?[A-Z][A-Za-z]*){0,3}"
_TITLE_RE = re.compile(
    rf"\b(?:Chief\s+[A-Za-z]+\s+Officer|Senior\s+Vice\s+President|Vice\s+President(?:\s+{_DEPT})?|"
    rf"VP(?:\s+{_DEPT})?|Managing\s+Director|General\s+Manager|"
    rf"Head\s+{_DEPT}|Director\s+{_DEPT})\b"
)


def scan(text: str) -> list[tuple[str, str]]:
    """(surface, entity_type) pairs for COMMERCIAL_TERM and
    CLIENT_PERSON_TITLE candidates found in `text`."""
    found: list[tuple[str, str]] = []
    for m in _CURRENCY_RE.finditer(text):
        found.append((m.group(0).strip(), "COMMERCIAL_TERM"))
    for m in _PAYMENT_TERM_KEYWORDS_RE.finditer(text):
        found.append((m.group(0).strip(), "COMMERCIAL_TERM"))
    for m in _TITLE_RE.finditer(text):
        found.append((m.group(0).strip(), "CLIENT_PERSON_TITLE"))
    return found
