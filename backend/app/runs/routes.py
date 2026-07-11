import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.db import get_db
from app.models import AgentRun, User
from app.runs.service import UnknownAgentError, execute_run
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
    try:
        run = await execute_run(db, agent_id=payload.agent_id, input_data=payload.input, created_by=user.id)
    except UnknownAgentError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

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
        RunSummaryOut(id=r.id, agent_id=r.agent_id, status=r.status, created_at=r.created_at, completed_at=r.completed_at)
        for r in runs
    ]


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> RunOut:
    run = (
        db.query(AgentRun)
        .options(joinedload(AgentRun.steps), joinedload(AgentRun.flags))
        .filter(AgentRun.id == run_id, AgentRun.created_by == user.id)
        .first()
    )
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return _to_run_out(run)
