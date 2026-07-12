import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, REAL, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    role_id: Mapped[int] = mapped_column(Integer, ForeignKey("roles.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    role: Mapped["Role"] = relationship("Role")


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    input_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    output_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    output_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    steps: Mapped[list["RunStep"]] = relationship(
        "RunStep", back_populates="run", order_by="RunStep.step_order", cascade="all, delete-orphan"
    )
    flags: Mapped[list["RunFlag"]] = relationship("RunFlag", back_populates="run", cascade="all, delete-orphan")


class RunStep(Base):
    __tablename__ = "run_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"))
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    run: Mapped["AgentRun"] = relationship("AgentRun", back_populates="steps")


class RunFlag(Base):
    __tablename__ = "run_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False, default="warning")

    run: Mapped["AgentRun"] = relationship("AgentRun", back_populates="flags")


class ReviewItem(Base):
    __tablename__ = "review_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id"), nullable=True)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    decision: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The reviewer's raw edits payload (removed_surfaces, excluded/included
    # image groups, masking_style, ...) - previously discarded entirely,
    # which made "did the reviewer actually approve this image?" unanswerable
    # after the fact. Write-only for audit; apply() still reads edits from
    # the request itself, not from here.
    edits_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id"), nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class ClientAccount(Base):
    """Placeholder client-account registry. Sanitization (A-01) will later link
    masked documents here via its source registry; for now this only exists so
    practice-lead account ownership has something to reference."""

    __tablename__ = "client_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    owners: Mapped[list["AccountOwnership"]] = relationship("AccountOwnership", back_populates="account", cascade="all, delete-orphan")


class AccountOwnership(Base):
    __tablename__ = "account_ownership"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    client_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("client_accounts.id", ondelete="CASCADE"), nullable=False
    )
    assigned_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    account: Mapped["ClientAccount"] = relationship("ClientAccount", back_populates="owners")
    user: Mapped["User"] = relationship("User")


# ---------------------------------------------------------------------------
# A-01 Sanitization + A-02 Tagging (built together)
# ---------------------------------------------------------------------------


class UploadedDocument(Base):
    """A raw document uploaded for sanitization. Stored on local disk; only
    Sanitization ever reads it via the fs.read_document MCP tool."""

    __tablename__ = "uploaded_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    filename: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class MaskingEntity(Base):
    """One canonical client-identifying entity with a GLOBAL, stable mask token
    reused across every document (decision: global mask scope). Grows by
    KM-governance approval - new entities land as pending_approval."""

    __tablename__ = "masking_entities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    entity_type: Mapped[str] = mapped_column(String, nullable=False)  # CLIENT_NAME, CLIENT_PERSON, ...
    mask_token: Mapped[str] = mapped_column(String, unique=True, nullable=False)  # e.g. [CLIENT_1]
    client_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("client_accounts.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending_approval")  # pending_approval | approved
    created_by_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    aliases: Mapped[list["MaskingAlias"]] = relationship(
        "MaskingAlias", back_populates="entity", cascade="all, delete-orphan"
    )
    client_account: Mapped["ClientAccount | None"] = relationship("ClientAccount")


class MaskingAlias(Base):
    """A surface form ("Acme", "Acme Corp", "ACME") mapping to one canonical
    entity. normalized_key is the lowercased/stripped match key."""

    __tablename__ = "masking_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("masking_entities.id", ondelete="CASCADE"), nullable=False)
    raw_value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    entity: Mapped["MaskingEntity"] = relationship("MaskingEntity", back_populates="aliases")


class LogoReference(Base):
    """Perceptual hash of a reviewer-confirmed client logo, linked to the same
    canonical entity as its text mask token (global, reused across documents -
    same pattern as MaskingAlias, but for images with no readable text).
    Auto-populated on approved image redactions; never manually curated."""

    __tablename__ = "logo_references"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mask_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("masking_entities.id", ondelete="CASCADE"), nullable=False)
    phash: Mapped[str] = mapped_column(String, nullable=False)
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class MaskingOccurrence(Base):
    """Where a masked entity appeared in a run's document - the exact span used
    for deterministic application and audit."""

    __tablename__ = "masking_occurrences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("masking_entities.id"), nullable=True)
    chunk_id: Mapped[int] = mapped_column(Integer, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    surface_text: Mapped[str] = mapped_column(Text, nullable=False)


class SourceRegistry(Base):
    """Write-only capture of real client identity. Written by the agent, never
    read back into output_json - the protected identity path."""

    __tablename__ = "source_registry"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    client_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("client_accounts.id"), nullable=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id"), nullable=False)
    raw_identity_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class DocumentMetadata(Base):
    """Sanitized summary + tag-relevant hints produced by Sanitization's
    Summarizer and consumed by Tagging. Contains NO client identity."""

    __tablename__ = "document_metadata"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), unique=True, nullable=False)
    sanitized_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class TagVocabulary(Base):
    """The controlled tag vocabulary, governed by KM-governance. Grows by
    approval - practice-lead proposals land as pending_approval."""

    __tablename__ = "tag_vocabulary"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    category: Mapped[str] = mapped_column(String, nullable=False)  # domain | use_case | technology | geography | engagement_type
    value: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="approved")  # approved | pending_approval
    proposed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("category", "value", name="uq_tag_category_value"),)


class RunTag(Base):
    """A tag attached to a Tagging run's document."""

    __tablename__ = "run_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    tag_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tag_vocabulary.id"), nullable=False)
    confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="applied")  # applied | flagged | confirmed
