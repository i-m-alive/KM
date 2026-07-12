import json
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, joinedload

from app.agents.registry import get_agent, is_background
from app.auth.deps import get_current_user
from app.auth.permissions import has_capability
from app.db import get_db
from app.documents.convert import ConversionUnavailableError, to_pdf_cached
from app.models import AgentRun, User
from app.runs.service import RoleNotAllowedError, UnknownAgentError, execute_run
from app.schemas import FlagOut, RunCreateRequest, RunOut, RunSummaryOut, StepOut

router = APIRouter(prefix="/runs", tags=["runs"])


def _to_run_out(run: AgentRun) -> RunOut:
    return RunOut(
        id=run.id,
        agent_id=run.agent_id,
        status=run.status,
        input=run.input_json,
        output=run.output_json,
        confidence=run.confidence,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        estimated_cost_usd=float(run.estimated_cost_usd) if run.estimated_cost_usd is not None else None,
        output_file_path=run.output_file_path,
        created_at=run.created_at,
        completed_at=run.completed_at,
        steps=[
            StepOut(order=s.step_order, name=s.name, detail=s.detail, tool=s.tool, duration_ms=s.duration_ms)
            for s in run.steps
        ],
        flags=[FlagOut(message=f.message, severity=f.severity) for f in run.flags],
    )


@router.post("", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def create_run(
    payload: RunCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RunOut:
    agent = get_agent(payload.agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No agent registered with id '{payload.agent_id}'")

    allowed = getattr(agent, "allowed_roles", []) or []
    if allowed and user.role.name not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Role '{user.role.name}' cannot trigger '{payload.agent_id}'")

    # Background agents are executed asynchronously by the worker; return the
    # pending run immediately and let the client poll GET /runs/{id}.
    if is_background(agent):
        run = AgentRun(agent_id=payload.agent_id, status="pending", input_json=payload.input, created_by=user.id)
        db.add(run)
        db.commit()
        db.refresh(run)
        return _to_run_out(run)

    # Interactive agents (e.g. dummy-echo) run synchronously in-request.
    try:
        run = await execute_run(
            db,
            agent_id=payload.agent_id,
            input_data=payload.input,
            created_by=user.id,
            created_by_role=user.role.name,
        )
    except UnknownAgentError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except RoleNotAllowedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    return _to_run_out(run)


@router.get("", response_model=list[RunSummaryOut])
def list_runs(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[RunSummaryOut]:
    runs = (
        db.query(AgentRun)
        .filter(AgentRun.created_by == user.id)
        .order_by(AgentRun.created_at.desc())
        .all()
    )
    return [
        RunSummaryOut(
            id=r.id, agent_id=r.agent_id, status=r.status, created_at=r.created_at, completed_at=r.completed_at,
            input_tokens=r.input_tokens, output_tokens=r.output_tokens,
            estimated_cost_usd=float(r.estimated_cost_usd) if r.estimated_cost_usd is not None else None,
        )
        for r in runs
    ]


@router.get("/{run_id}/masked")
def get_masked_document(run_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    """Return the full masked (sanitized) document for a completed Sanitization
    run — the masked chunks written to the run's output file on approval."""
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if run.created_by != user.id and not has_capability(user, "review_queue_manage"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed to view this document")
    if run.agent_id != "sanitization":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only Sanitization runs have a masked document")
    if run.status not in ("completed", "completed_with_issues"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run is '{run.status}', not completed")
    if not run.output_file_path or not os.path.exists(run.output_file_path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Masked document not found on disk")

    with open(run.output_file_path) as f:
        data = json.load(f)
    chunks = data.get("masked_chunks", [])
    return {
        "filename": data.get("filename"),
        "chunks": chunks,
        "masked_text": "\n\n".join(c.get("text", "") for c in chunks),
        "entities_masked": data.get("entities_masked", []),
    }


@router.get("/{run_id}/masked/download")
def download_masked_document(run_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Download the sanitized document in its original format (masked PDF/DOCX/PPTX/XLSX)."""
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if run.created_by != user.id and not has_capability(user, "review_queue_manage"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed to download this document")
    path = (run.output_json or {}).get("masked_document_path") if isinstance(run.output_json, dict) else None
    if not path or not os.path.exists(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Masked document not found on disk")
    return FileResponse(path, filename=os.path.basename(path))


@router.get("/{run_id}/masked/preview")
def preview_masked_document(run_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """PDF preview of the MASKED document (converted via LibreOffice if it
    isn't already a PDF) - the counterpart to /documents/{id}/preview, so the
    frontend can render original vs. sanitized side by side."""
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if run.created_by != user.id and not has_capability(user, "review_queue_manage"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed to view this document")
    path = (run.output_json or {}).get("masked_document_path") if isinstance(run.output_json, dict) else None
    if not path or not os.path.exists(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Masked document not found on disk")

    try:
        pdf_path = to_pdf_cached(path)
    except ConversionUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    return FileResponse(pdf_path, media_type="application/pdf")


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> RunOut:
    run = (
        db.query(AgentRun)
        .options(joinedload(AgentRun.steps), joinedload(AgentRun.flags))
        .filter(AgentRun.id == run_id)
        .first()
    )
    if run is None or (run.created_by != user.id and not has_capability(user, "review_queue_manage")):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return _to_run_out(run)
