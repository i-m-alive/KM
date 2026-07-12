"""Shared regex pattern builder for masking. A bare substring match (no word
boundaries) will replace a surface string WHEREVER it appears, including
mid-word - a short surface like "RIA" or "sure" then corrupts ordinary
prose ("Va[RIA]nce", "expo[sure]") instead of only matching the standalone
term. Every regex-based masking site must build patterns the same way, or
detection/masking/verification can silently disagree on what "matches".
"""

import re


def surface_pattern(surface: str) -> str:
    return r"\b" + re.escape(surface) + r"\b"
