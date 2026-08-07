"""Per-entity-type default action (Phase 3) - the load-bearing distinction
that decides what happens to an entity if the reviewer never touches it.

Three values, same three-way split the taxonomy has used since Phase 2's
CREDENTIAL:
  "mask"      - included/masked by DEFAULT; reviewer opts OUT via the
                existing removed_surfaces edit (Phase 1/2 behavior,
                unchanged - every type not listed below keeps this default).
  "flag"      - EXCLUDED by default; a case study's whole value is in real
                outcomes, numbers, and methodology, so auto-masking a
                commercial figure or a competitor mention would silently
                gut its credibility. Reviewer opts IN via a new
                included_surfaces edit (see agent.py's
                _resolve_entity_inclusion) to actually mask it.
  "mandatory" - always masked; reviewer cannot opt out (Phase 2's
                CREDENTIAL, unchanged).

INTERNAL_TEAM_MEMBER is deliberately NOT a static entry here - its default
depends on a per-person consent lookup (see resolve_default_action), not a
fixed per-type rule.
"""

FLAG_DEFAULT_TYPES = {
    "COMMERCIAL_TERM",
    "COMPETITOR_NAME",
    "STRATEGY_MENTION",
    "OWN_COST_DETAIL",
    "ORG_CHART_STRUCTURE",
}

MANDATORY_TYPES = {"CREDENTIAL"}


def resolve_default_action(entity_type: str, consent_status: str | None = None) -> str:
    """The default action for one entity, given its type and (for
    INTERNAL_TEAM_MEMBER only) its consent lookup result. Every other type
    ignores `consent_status` entirely.

    INTERNAL_TEAM_MEMBER: granted consent -> "keep" (excluded by default,
    same shape as "flag" - the person chose to be visible). Anything else
    (pending, not_required-but-unset, or no record at all) -> "mask" by
    default - absent an explicit yes, the safer failure is over-redacting a
    colleague's name/photo, not under-redacting it (the same "a missed
    identifier is worse than an over-cautious mask" reasoning already
    applied everywhere else in this taxonomy, just pointed at our own
    people instead of the client's)."""
    if entity_type == "INTERNAL_TEAM_MEMBER":
        return "keep" if consent_status == "granted" else "mask"
    if entity_type in MANDATORY_TYPES:
        return "mandatory"
    if entity_type in FLAG_DEFAULT_TYPES:
        return "flag"
    return "mask"
