"""Shared regex pattern builder for masking. A bare substring match (no word
boundaries) will replace a surface string WHEREVER it appears, including
mid-word - a short surface like "RIA" or "sure" then corrupts ordinary
prose ("Va[RIA]nce", "expo[sure]") instead of only matching the standalone
term. Every regex-based masking site must build patterns the same way, or
detection/masking/verification can silently disagree on what "matches".
"""

import re

# Regex \b is a transition between \w and non-\w - and \w includes "_". That
# makes \b blind to a real boundary at an underscore: \bNextCare\b does NOT
# match "NextCare_logo" (observed real leak: alt-text filenames/descr values
# routinely look like "Nextcare_logo" or "some_file_name.png"), regardless
# of case - this was originally misdiagnosed as a case-sensitivity bug, but
# case-insensitive matching was already in place everywhere (re.IGNORECASE)
# and didn't fix it, because the actual blocker is the boundary check itself
# never firing. A negative lookaround against alphanumerics only (not "_")
# treats "_" as a valid separator while still rejecting a match embedded in
# an actual alphanumeric run (e.g. "RIA" must still not match inside
# "MATERIAL").
_BOUNDARY = r"(?<![A-Za-z0-9])"
_BOUNDARY_END = r"(?![A-Za-z0-9])"


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
    return _BOUNDARY + re.escape(surface).replace(re.escape(" "), r"\s+") + _BOUNDARY_END
