"""source_registry + client_account — PROTECTED calls (agent code only).

Real client identity is captured write-only: written here, never read back into
output_json. client_account links a run to a (placeholder) client account so
"organise by client" works through the protected path only.
"""

import uuid

from sqlalchemy.orm import Session

from app.models import ClientAccount, SourceRegistry


def capture_identity(db: Session, run_id: uuid.UUID, raw_identity: dict, client_account_id: uuid.UUID | None) -> None:
    """Write-only capture of the real identity behind the masks."""
    db.add(
        SourceRegistry(
            run_id=run_id,
            client_account_id=client_account_id,
            raw_identity_json=raw_identity,
        )
    )
    db.flush()


def get_or_create_client_account(db: Session, name: str) -> ClientAccount:
    existing = db.query(ClientAccount).filter(ClientAccount.name == name).first()
    if existing is not None:
        return existing
    account = ClientAccount(name=name)
    db.add(account)
    db.flush()
    return account
