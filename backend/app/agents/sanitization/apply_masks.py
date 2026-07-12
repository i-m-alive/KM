"""Deterministic mask application. The model proposed which strings are
client-identifying; this finds every occurrence and replaces it with the
entity's global mask token. Exact and repeatable — never an LLM pass."""

import re
from dataclasses import dataclass

from app.documents.extract import Chunk
from app.masking.pattern import surface_pattern
from app.masking.style import DEFAULT_MASKING_STYLE, replacement_for


@dataclass
class Occurrence:
    chunk_id: int
    start_offset: int
    end_offset: int
    surface_text: str
    mask_token: str


def apply_masks(
    chunks: list[Chunk], surface_to_token: dict[str, str], style: str = DEFAULT_MASKING_STYLE
) -> tuple[list[dict], list[Occurrence]]:
    """Replace every occurrence of each surface (case-insensitive, longest-first)
    with its mask token (or a black-block / empty string, per `style`). Returns
    (masked_chunks, occurrences)."""
    # Longest surfaces first so "Acme Corporation" is masked before "Acme".
    surfaces = sorted(surface_to_token.keys(), key=len, reverse=True)
    occurrences: list[Occurrence] = []
    masked_chunks: list[dict] = []

    for chunk in chunks:
        text = chunk.text
        # Record occurrences against the ORIGINAL text offsets, then rebuild masked text.
        spans: list[tuple[int, int, str, str]] = []  # (start, end, surface, token)
        claimed: list[tuple[int, int]] = []

        for surface in surfaces:
            token = surface_to_token[surface]
            for m in re.finditer(surface_pattern(surface), text, flags=re.IGNORECASE):
                s, e = m.start(), m.end()
                # skip if overlaps a longer, already-claimed span
                if any(not (e <= cs or s >= ce) for cs, ce in claimed):
                    continue
                claimed.append((s, e))
                spans.append((s, e, text[s:e], token))

        spans.sort(key=lambda x: x[0])
        for s, e, surface_actual, token in spans:
            occurrences.append(
                Occurrence(chunk_id=chunk.chunk_id, start_offset=s, end_offset=e, surface_text=surface_actual, mask_token=token)
            )

        # Rebuild masked text from the spans.
        out = []
        cursor = 0
        for s, e, surface_actual, token in spans:
            out.append(text[cursor:s])
            out.append(replacement_for(surface_actual, token, style))
            cursor = e
        out.append(text[cursor:])
        masked_chunks.append({"chunk_id": chunk.chunk_id, "label": chunk.label, "text": "".join(out)})

    return masked_chunks, occurrences
