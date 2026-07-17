"""Deterministic mask application. The model proposed which strings are
client-identifying; this finds every occurrence and replaces it with the
entity's global mask token. Exact and repeatable — never an LLM pass."""

from dataclasses import dataclass

from app.documents.extract import Chunk
from app.masking.spans import resolve_mask_spans
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
    (masked_chunks, occurrences). Span resolution (longest-first, non-
    overlapping-claim) is shared with render.py via app.masking.spans, so this
    and the native-format renderer can never disagree about what counts as a
    match."""
    occurrences: list[Occurrence] = []
    masked_chunks: list[dict] = []

    for chunk in chunks:
        text = chunk.text
        spans = resolve_mask_spans(text, surface_to_token)
        for sp in spans:
            occurrences.append(
                Occurrence(
                    chunk_id=chunk.chunk_id, start_offset=sp.start, end_offset=sp.end,
                    surface_text=sp.surface, mask_token=sp.token,
                )
            )

        out = []
        cursor = 0
        for sp in spans:
            out.append(text[cursor : sp.start])
            out.append(replacement_for(sp.surface, sp.token, style))
            cursor = sp.end
        out.append(text[cursor:])
        masked_chunks.append({"chunk_id": chunk.chunk_id, "label": chunk.label, "text": "".join(out)})

    return masked_chunks, occurrences
