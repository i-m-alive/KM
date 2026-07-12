import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.permissions import require_capability
from app.db import get_db
from app.models import AuditLog, Role, User
from app.schemas import AuditLogEntryOut, RoleUpdateRequest, UserOut

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=list[UserOut])
def list_users(
    _: User = Depends(require_capability("manage_users")),
    db: Session = Depends(get_db),
) -> list[UserOut]:
    users = db.query(User).order_by(User.created_at.desc()).all()
    return [UserOut(id=u.id, email=u.email, role=u.role.name, created_at=u.created_at) for u in users]


@router.patch("/users/{user_id}/role", response_model=UserOut)
def update_user_role(
    user_id: uuid.UUID,
    payload: RoleUpdateRequest,
    actor: User = Depends(require_capability("manage_users")),
    db: Session = Depends(get_db),
) -> UserOut:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    new_role = db.query(Role).filter(Role.name == payload.role).first()
    if new_role is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown role '{payload.role}'")

    old_role_name = user.role.name
    user.role_id = new_role.id
    db.add(AuditLog(actor_id=actor.id, action=f"role_changed: {user.email} {old_role_name} -> {new_role.name}"))
    db.commit()

    return UserOut(id=user.id, email=user.email, role=new_role.name, created_at=user.created_at)


@router.get("/audit-log", response_model=list[AuditLogEntryOut])
def get_admin_audit_log(
    _: User = Depends(require_capability("view_audit_log_user_mgmt")),
    db: Session = Depends(get_db),
) -> list[AuditLogEntryOut]:
    # Admin's audit visibility is scoped to user-management actions only,
    # per the RBAC matrix - full visibility is a km_governance capability.
    entries = (
        db.query(AuditLog)
        .filter(AuditLog.action.like("role_changed:%"))
        .order_by(AuditLog.created_at.desc())
        .all()
    )
    return [
        AuditLogEntryOut(
            id=e.id,
            run_id=e.run_id,
            actor_id=e.actor_id,
            actor_email=e.actor_id and db.get(User, e.actor_id).email,
            action=e.action,
            created_at=e.created_at,
        )
        for e in entries
    ]
