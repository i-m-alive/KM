"""Reviewer-edit deltas as precision/recall signal (Task 3).

A reviewer ADDING an entity the model missed entirely is a recall failure; a
reviewer REMOVING one the model over-flagged is a precision failure. Tagged
by entity_type so per-type threshold tuning has real per-run data to work
from instead of one global guess (see config.SANITIZATION_CONFIDENCE_THRESHOLDS).

The reviewer's raw edits were already being written to ReviewItem.edits_json
for audit, but write-only - nothing counted or aggregated them. These are
pure, DB-free functions specifically so this counting logic is unit-testable
without a full apply() run.
"""

from app.masking import dictionary


def precision_miss_by_type(proposal_entities: list[dict], removed: set[str]) -> dict[str, int]:
    """`removed` is the set of lowercased surface strings the reviewer struck
    from the proposal - the model over-flagged them, a precision failure."""
    counts: dict[str, int] = {}
    for e in proposal_entities:
        if e["surface_text"].lower() in removed:
            etype = e.get("entity_type", "CLIENT_NAME")
            counts[etype] = counts.get(etype, 0) + 1
    return counts


def resolve_added_entities(added_entities: list[dict], existing_keys: set[str]) -> tuple[list[dict], dict[str, int]]:
    """Dedupe reviewer-added entities against `existing_keys` (normalized
    surfaces the model already proposed) - the same normalized-key logic
    apply() uses everywhere else. Returns (new_entities, recall_miss_by_type):
    `new_entities` is what the caller should actually append to the masking
    set (a real miss the model made); an "added" entity whose normalized key
    already exists is a redundant edit, not a miss, and is silently dropped
    from both the return list and the counts - it costs nothing and
    shouldn't count as evidence the model under-detected."""
    seen = set(existing_keys)
    new_entities: list[dict] = []
    counts: dict[str, int] = {}
    for added in added_entities:
        surface = (added.get("surface_text") or "").strip()
        if not surface:
            continue
        key = dictionary.normalize(surface)
        if key in seen:
            continue
        seen.add(key)
        etype = added.get("entity_type") or "CLIENT_NAME"
        counts[etype] = counts.get(etype, 0) + 1
        new_entities.append({"surface_text": surface, "entity_type": etype})
    return new_entities, counts
