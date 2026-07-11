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


class RunOut(BaseModel):
    id: uuid.UUID
    agent_id: str
    status: Literal["pending", "running", "completed", "failed"]
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
    status: Literal["pending", "running", "completed", "failed"]
    created_at: datetime
    completed_at: datetime | None
