import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.agents.registry import get_agent, is_background
from app.auth.permissions import require_capability
from app.db import get_db
from app.models import AgentRun, AuditLog, ReviewItem, User
from app.runs.background import mark_failed, mark_rejected, run_application
from app.schemas import (
    FlagOut,
    ReviewDecisionRequest,
    ReviewDetailOut,
    ReviewQueueItemOut,
    RunOut,
    StepOut,
)

router = APIRouter(prefix="/review", tags=["review"])


@router.get("/queue", response_model=list[ReviewQueueItemOut])
def review_queue(
    _: User = Depends(require_capability("review_queue_manage")),
    db: Session = Depends(get_db),
) -> list[ReviewQueueItemOut]:
    runs = (
        db.query(AgentRun)
        .filter(AgentRun.status == "awaiting_review")
        .order_by(AgentRun.created_at.asc())
        .all()
    )
    out = []
    for r in runs:
        creator = db.get(User, r.created_by)
        summary = (r.output_json or {}).get("summary") if isinstance(r.output_json, dict) else None
        out.append(
            ReviewQueueItemOut(
                run_id=r.id,
                agent_id=r.agent_id,
                summary=summary,
                created_by_email=creator.email if creator else None,
                created_at=r.created_at,
            )
        )
    return out


@router.get("/{run_id}", response_model=ReviewDetailOut)
def review_detail(
    run_id: uuid.UUID,
    _: User = Depends(require_capability("review_queue_manage")),
    db: Session = Depends(get_db),
) -> ReviewDetailOut:
    run = (
        db.query(AgentRun)
        .options(joinedload(AgentRun.steps), joinedload(AgentRun.flags))
        .filter(AgentRun.id == run_id)
        .first()
    )
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    oj = run.output_json if isinstance(run.output_json, dict) else {}
    return ReviewDetailOut(
        run_id=run.id,
        agent_id=run.agent_id,
        status=run.status,
        summary=oj.get("summary"),
        proposal=oj.get("proposal"),
        steps=[StepOut(order=s.step_order, name=s.name, detail=s.detail, tool=s.tool, duration_ms=s.duration_ms) for s in run.steps],
        flags=[FlagOut(message=f.message, severity=f.severity) for f in run.flags],
    )


@router.post("/{run_id}", response_model=RunOut)
async def submit_review(
    run_id: uuid.UUID,
    payload: ReviewDecisionRequest,
    reviewer: User = Depends(require_capability("review_queue_manage")),
    db: Session = Depends(get_db),
) -> RunOut:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if run.status != "awaiting_review":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run is '{run.status}', not awaiting_review")

    # Separation of duty: the identity that produced a run never approves it.
    # admin is exempt (testing superuser) so a single account can drive the
    # whole flow end-to-end.
    if run.created_by == reviewer.id and reviewer.role.name != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot review your own run")

    agent = get_agent(run.agent_id)
    if agent is None or not is_background(agent):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Run's agent is not a reviewable background agent")

    # Sensitive Outcomes (Phase 3): a non-dismissible gate, not just a UI
    # callout - a reviewer approving/editing a run that discusses a
    # negative outcome MUST record how they're handling it. Rejection is
    # exempt (killing the run is itself a resolution). Enforced server-side,
    # same "not just discouraged in the UI" discipline as CREDENTIAL's
    # mandatory masking (Phase 2).
    proposal_for_gate = (run.output_json or {}).get("proposal") if isinstance(run.output_json, dict) else None
    _valid_outcome_resolutions = {"approve_as_is", "requires_client_signoff", "remove_section"}
    negative_outcome_resolution = (payload.edits or {}).get("negative_outcome_resolution") if payload.edits else None
    if (
        payload.decision != "rejected"
        and isinstance(proposal_for_gate, dict)
        and proposal_for_gate.get("discusses_negative_outcome")
        and negative_outcome_resolution not in _valid_outcome_resolutions
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This document discusses a sensitive outcome - set edits.negative_outcome_resolution to one of "
            "'approve_as_is', 'requires_client_signoff', or 'remove_section' before approving.",
        )

    # Record the reviewer decision on the (agent-filed) review item, or create one.
    item = db.query(ReviewItem).filter(ReviewItem.run_id == run.id).order_by(ReviewItem.id.desc()).first()
    if item is None:
        item = ReviewItem(run_id=run.id)
        db.add(item)
    item.reviewer_id = reviewer.id
    item.decision = payload.decision
    item.notes = payload.notes
    item.edits_json = payload.edits
    item.decided_at = datetime.utcnow()
    db.add(AuditLog(run_id=run.id, actor_id=reviewer.id, action=f"review_{payload.decision}: {run.agent_id}"))
    if negative_outcome_resolution in _valid_outcome_resolutions:
        # Directly satisfies checklist item 7.2 ("checked for client
        # approval before sharing exact figures") - this record IS the
        # approval trail: who resolved it, how, and when.
        db.add(AuditLog(
            run_id=run.id, actor_id=reviewer.id,
            action=f"negative_outcome_resolution: {negative_outcome_resolution}",
        ))
    db.commit()

    if payload.decision == "rejected":
        mark_rejected(db, run, payload.notes)
    else:
        proposal = (run.output_json or {}).get("proposal") if isinstance(run.output_json, dict) else None
        decision = {"decision": payload.decision, "proposal": proposal, "edits": payload.edits or {}}
        # apply() calling out to Bedrock (summarizer, revalidation, ...) can
        # fail for reasons that have nothing to do with the reviewer's
        # request (expired credentials, a transient throttle) - unlike the
        # worker's run_detection path (worker.py's _process(), which already
        # wraps this in mark_failed), this call was previously unguarded, so
        # any such failure surfaced as an opaque 500 instead of a normal
        # "failed" run the reviewer can see and retry. Same safety net,
        # applied to the synchronous review-triggered path.
        try:
            await run_application(db, run, agent, decision)
        except Exception as exc:
            mark_failed(db, run, str(exc))

    db.refresh(run)
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
        steps=[StepOut(order=s.step_order, name=s.name, detail=s.detail, tool=s.tool, duration_ms=s.duration_ms) for s in run.steps],
        flags=[FlagOut(message=f.message, severity=f.severity) for f in run.flags],
    )
