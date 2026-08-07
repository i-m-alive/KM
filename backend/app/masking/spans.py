"""Single source of truth for "what counts as a match" when applying masks -
shared by apply_masks.py (chunk-text masking, used for the preview/.txt
channel and MaskingOccurrence rows) and every run-boundary-aware rewrite site
(render.py's paragraph masking, pptx_richcontent.py's SmartArt masking).
Before this existed, each call site re-implemented longest-surface-first,
non-overlapping span resolution independently - they happened to agree by
convention, not by construction, and could silently drift the next time only
one of them was touched.
"""

import re
from dataclasses import dataclass

from app.masking.pattern import surface_pattern
from app.masking.style import replacement_for


@dataclass
class MaskSpan:
    start: int
    end: int
    surface: str  # the ACTUAL matched text (case/whitespace exactly as it appears in the source)
    token: str


def resolve_mask_spans(text: str, surface_to_token: dict[str, str]) -> list[MaskSpan]:
    """Every non-overlapping occurrence of any surface in `text`, longest-
    surface-first (so "Acme Corporation" claims its span before "Acme" can
    claim part of it), case-insensitive, word-boundary matched (surface_pattern
    also tolerates a wrapped whitespace run inside a multi-word surface).
    Returned sorted by position."""
    if not text or not surface_to_token:
        return []
    surfaces = sorted(surface_to_token.keys(), key=len, reverse=True)
    claimed: list[tuple[int, int]] = []
    spans: list[MaskSpan] = []
    for surface in surfaces:
        token = surface_to_token[surface]
        for m in re.finditer(surface_pattern(surface), text, flags=re.IGNORECASE):
            s, e = m.start(), m.end()
            # Skip if this candidate overlaps a longer, already-claimed span.
            if any(not (e <= cs or s >= ce) for cs, ce in claimed):
                continue
            claimed.append((s, e))
            spans.append(MaskSpan(s, e, text[s:e], token))
    spans.sort(key=lambda sp: sp.start)
    return spans


def apply_spans_to_runs(runs: list, surface_to_token: dict[str, str], style: str) -> bool:
    """Rewrite a sequence of run-like objects - anything with a gettable/
    settable `.text` attribute, e.g. a python-pptx/python-docx `_Run`, or a
    raw lxml element such as DrawingML's `a:t` (SmartArt text) - so their
    concatenated text is masked, preserving whatever content sits outside a
    match. A match entirely inside one run is substituted in that run
    alone. A match spanning multiple runs only touches the runs it actually
    covers: the first touched run keeps its own pre-match prefix and gains
    the replacement text (so it carries that run's formatting/properties -
    the same choice PowerPoint's own multi-run edits imply), any runs
    strictly between are cleared (their entire text was inside the match,
    so there's nothing of theirs left to preserve), and the last touched
    run keeps its own post-match suffix. Every run outside the match span
    is never touched. This is what replaced an earlier approach (in both
    render.py and, initially, SmartArt masking) of collapsing the whole
    masked text into the first run and blanking every other one, which
    destroyed formatting/content on runs that had nothing to do with the
    match. Returns True if anything changed."""
    if not runs:
        return False
    run_texts = [r.text or "" for r in runs]
    joined = "".join(run_texts)
    if not joined:
        return False
    spans = resolve_mask_spans(joined, surface_to_token)
    if not spans:
        return False

    # Each run's [start, end) offset in the ORIGINAL joined text - fixed for
    # the whole call; only run_texts (the current per-run string) mutates.
    run_bounds = []
    pos = 0
    for t in run_texts:
        run_bounds.append((pos, pos + len(t)))
        pos += len(t)

    def run_index_at(offset: int) -> int:
        for i, (s, e) in enumerate(run_bounds):
            if s <= offset < e:
                return i
        return len(run_bounds) - 1

    # Apply back-to-front (highest offset first): editing a run's text at a
    # given position never changes the validity of any offset strictly to
    # its left, which is exactly every span still left to process, and
    # remains true even when two spans land in the SAME run (that run's text
    # has already been updated by the time the earlier span is processed, but
    # the earlier span's own local offsets are still all <= the point where
    # the later edit happened, so slicing the CURRENT text at those offsets
    # is still correct).
    for sp in reversed(spans):
        replacement = replacement_for(sp.surface, sp.token, style)
        start_idx = run_index_at(sp.start)
        end_idx = run_index_at(sp.end - 1)
        if start_idx == end_idx:
            local_start = sp.start - run_bounds[start_idx][0]
            local_end = sp.end - run_bounds[start_idx][0]
            current = run_texts[start_idx]
            new_text = current[:local_start] + replacement + current[local_end:]
            runs[start_idx].text = new_text
            run_texts[start_idx] = new_text
        else:
            first_local_start = sp.start - run_bounds[start_idx][0]
            last_local_end = sp.end - run_bounds[end_idx][0]
            prefix = run_texts[start_idx][:first_local_start]
            suffix = run_texts[end_idx][last_local_end:]
            new_first = prefix + replacement
            runs[start_idx].text = new_first
            run_texts[start_idx] = new_first
            for mid in range(start_idx + 1, end_idx):
                runs[mid].text = ""
                run_texts[mid] = ""
            runs[end_idx].text = suffix
            run_texts[end_idx] = suffix
    return True
