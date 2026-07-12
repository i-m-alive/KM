import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field


# ---- Auth ----


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user: UserOut


# ---- Agent catalogue ----


class AgentOut(BaseModel):
    agent_id: str
    display_name: str
    description: str
    mode: Literal["background", "interactive"]
    tools: list[str]


# ---- Runs ----


class RunCreateRequest(BaseModel):
    agent_id: str
    input: dict[str, Any]


class StepOut(BaseModel):
    order: int
    name: str
    detail: str | None
    tool: str | None
    duration_ms: int | None

    model_config = {"from_attributes": True}


class FlagOut(BaseModel):
    message: str
    severity: Literal["info", "warning", "blocking"]

    model_config = {"from_attributes": True}


# Lifecycle: pending | working | detecting | tagging | awaiting_review | applying | completed | failed | rejected
RunStatus = str


class RunOut(BaseModel):
    id: uuid.UUID
    agent_id: str
    status: RunStatus
    input: dict[str, Any]
    output: dict[str, Any] | None
    confidence: float | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: float | None
    output_file_path: str | None
    created_at: datetime
    completed_at: datetime | None
    steps: list[StepOut]
    flags: list[FlagOut]


class RunSummaryOut(BaseModel):
    id: uuid.UUID
    agent_id: str
    status: RunStatus
    created_at: datetime
    completed_at: datetime | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: float | None


# ---- Admin ----


class RoleUpdateRequest(BaseModel):
    role: str


# ---- Governance (account ownership) ----


class ClientAccountCreateRequest(BaseModel):
    name: str = Field(min_length=1)


class OwnerAssignRequest(BaseModel):
    user_id: uuid.UUID


class ClientAccountOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    owners: list[UserOut]


class AuditLogEntryOut(BaseModel):
    id: int
    run_id: uuid.UUID | None
    actor_id: uuid.UUID | None
    actor_email: str | None
    action: str
    created_at: datetime


class MaskingEntityOut(BaseModel):
    id: uuid.UUID
    mask_token: str
    entity_type: str
    status: str  # pending_approval | approved | skipped
    aliases: list[str]
    client_account_id: uuid.UUID | None
    client_account_name: str | None
    created_at: datetime


# ---- Documents ----


class DocumentOut(BaseModel):
    id: uuid.UUID
    filename: str
    content_type: str
    uploaded_at: datetime
    chunk_count: int | None = None


# ---- Review ----


class ReviewQueueItemOut(BaseModel):
    run_id: uuid.UUID
    agent_id: str
    summary: str | None
    created_by_email: str | None
    created_at: datetime


class ReviewDetailOut(BaseModel):
    run_id: uuid.UUID
    agent_id: str
    status: RunStatus
    summary: str | None
    proposal: dict[str, Any] | None
    steps: list[StepOut]
    flags: list[FlagOut]


class ReviewDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected", "edited"]
    notes: str | None = None
    edits: dict[str, Any] | None = None  # agent-specific reviewer edits (e.g. removed entities/tags)


# ---- Tags ----


class TagVocabularyOut(BaseModel):
    id: uuid.UUID
    category: str
    value: str
    status: str
    created_at: datetime


class TagCreateRequest(BaseModel):
    category: str
    value: str


class RunTagOut(BaseModel):
    category: str
    value: str
    confidence: float | None
    status: str
