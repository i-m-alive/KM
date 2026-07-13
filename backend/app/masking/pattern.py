"""Shared regex pattern builder for masking. A bare substring match (no word
boundaries) will replace a surface string WHEREVER it appears, including
mid-word - a short surface like "RIA" or "sure" then corrupts ordinary
prose ("Va[RIA]nce", "expo[sure]") instead of only matching the standalone
term. Every regex-based masking site must build patterns the same way, or
detection/masking/verification can silently disagree on what "matches".
"""

import re


def surface_pattern(surface: str) -> str:
    # Interior spaces match ANY whitespace run (\s+), not just a single
    # literal space: extracted text preserves line breaks, so a multi-word
    # name wrapped across a line ("Tata\nCapital") must still match in
    # masking AND verification - with a literal space, the verifier was
    # blind to exactly the wrapped occurrences the renderer is most likely
    # to have missed.
    # re.escape may render a space as "\ " depending on Python version -
    # replace the ESCAPED form, not a bare " ", or the substitution corrupts
    # the pattern instead of loosening it.
    return r"\b" + re.escape(surface).replace(re.escape(" "), r"\s+") + r"\b"
