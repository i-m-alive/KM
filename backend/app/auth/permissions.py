from fastapi import Depends, HTTPException, status

from app.auth.deps import get_current_user
from app.models import User

# Direct transcription of the NaviKnow RBAC matrix. Single source of truth -
# routes gate on capabilities, never on hardcoded role lists, so this table
# is the only place the permission model needs to change.
#
# Scoped rows from the matrix ("own accounts", "propose", "user-mgmt only")
# aren't plain booleans - those get their own capability name and the
# endpoint applies the actual scoping (e.g. filtering by account_ownership).
ROLE_CAPABILITIES: dict[str, set[str]] = {
    "admin": {
        "manage_users",
        "browse_and_search",
        "view_audit_log_user_mgmt",
    },
    "km_governance": {
        "browse_and_search",
        "submit_documents",
        "use_deck_drafting",
        "view_all_runs_org_wide",
        "review_queue_manage",
        "approve_new_mask",
        "view_raw_masking_dictionary",
        "client_lookup_any",
        "assign_account_ownership",
        "manage_tag_taxonomy",
        "manage_eligibility_rules",
        "view_audit_log_full",
        "approve_cleanup_actions",
    },
    "km_reviewer": {
        "browse_and_search",
        "submit_documents",
        "use_deck_drafting",
        "review_queue_manage",
    },
    "practice_lead": {
        "browse_and_search",
        "submit_documents",
        "use_deck_drafting",
        "client_lookup_own",
        "propose_tag_taxonomy",
    },
    "delivery": {
        "browse_and_search",
        "submit_documents",
        "use_deck_drafting",
    },
    "read_only": {
        "browse_and_search",
    },
}


def has_capability(user: User, capability: str) -> bool:
    # admin is a testing superuser: it holds every capability so a single
    # account can exercise the whole pipeline (submit, review, govern). In a
    # real deployment, tighten this back to ROLE_CAPABILITIES["admin"].
    if user.role.name == "admin":
        return True
    return capability in ROLE_CAPABILITIES.get(user.role.name, set())


def require_capability(capability: str):
    def _check(user: User = Depends(get_current_user)) -> User:
        if not has_capability(user, capability):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Role '{user.role.name}' lacks the '{capability}' capability",
            )
        return user

    return _check
