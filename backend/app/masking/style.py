"""Reviewer-selectable sanitization style: how an identified surface is
replaced in the rendered/masked output. Chosen at review time (edits.masking_style),
applied uniformly to every entity in the run.
"""

MASKING_STYLES = {"token", "black", "remove"}
DEFAULT_MASKING_STYLE = "token"


def resolve_style(style: str | None) -> str:
    return style if style in MASKING_STYLES else DEFAULT_MASKING_STYLE


def replacement_for(surface: str, token: str, style: str) -> str:
    """The literal text that replaces `surface` in the rendered document."""
    if style == "remove":
        return ""
    if style == "black":
        return "█" * max(len(surface), 3)  # solid block chars, same visual weight as the redacted text
    return token  # "token" (default): the stable, traceable [CLIENT_N]-style mask
