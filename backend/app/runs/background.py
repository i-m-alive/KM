"""Orchestration for two-phase background agents (Sanitization, Tagging).

detect() -> proposal -> (human review unless auto-apply) -> apply() -> completed.
Shared by the worker (detection) and the review endpoint (application).
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.agents.base import AgentResult, BackgroundAgent, ReviewProposal
from app.models import AgentRun, RunFlag, RunStep


def _persist_steps(db: Session, run: AgentRun, steps, base_order: int = 0) -> int:
    order = base_order
    for step in steps:
        order += 1
        db.add(
            RunStep(
                run_id=run.id,
                step_order=step.order if step.order else order,
                name=step.name,
                detail=step.detail,
                tool=step.tool,
                duration_ms=step.duration_ms,
            )
        )
    return order


def _persist_flags(db: Session, run: AgentRun, flags) -> None:
    for flag in flags:
        db.add(RunFlag(run_id=run.id, message=flag.message, severity=flag.severity))


def _finalize_completed(db: Session, run: AgentRun, result: AgentResult) -> None:
    # A "blocking" flag exists to stop a run from being silently treated as
    # done - e.g. the masking verifier finding a client logo still sitting in
    # the "sanitized" file. Marking the run "completed" regardless of flag
    # severity (the previous behavior) made that flag purely cosmetic: the
    # file still looked done everywhere status is checked.
    has_blocking = any(f.severity == "blocking" for f in result.flags)
    run.status = "completed_with_issues" if has_blocking else "completed"
    run.output_json = result.output
    run.confidence = result.confidence
    run.input_tokens = (run.input_tokens or 0) + result.input_tokens
    run.output_tokens = (run.output_tokens or 0) + result.output_tokens
    run.estimated_cost_usd = float(run.estimated_cost_usd or 0) + result.estimated_cost_usd
    run.output_file_path = result.output_file_path
    run.completed_at = datetime.utcnow()
    existing = db.query(RunStep).filter(RunStep.run_id == run.id).count()
    _persist_steps(db, run, result.steps, base_order=existing)
    _persist_flags(db, run, result.flags)
    db.commit()


async def run_detection(db: Session, run: AgentRun, agent: BackgroundAgent) -> None:
    """Phase 1. On auto-apply proposals, chains straight into apply()."""
    proposal: ReviewProposal = await agent.detect(db, run)

    run.status = proposal.working_status  # transient label already used during detect; re-affirm
    run.input_tokens = (run.input_tokens or 0) + proposal.input_tokens
    run.output_tokens = (run.output_tokens or 0) + proposal.output_tokens
    run.estimated_cost_usd = float(run.estimated_cost_usd or 0) + proposal.estimated_cost_usd
    _persist_steps(db, run, proposal.steps)
    _persist_flags(db, run, proposal.flags)

    if proposal.needs_review:
        run.output_json = {"phase": "proposal", "summary": proposal.summary, "proposal": proposal.proposal}
        run.status = "awaiting_review"
        db.commit()
        return

    # Auto-apply path (no human needed).
    db.commit()
    result = await agent.apply(db, run, decision={"auto": True, "proposal": proposal.proposal})
    _finalize_completed(db, run, result)


async def run_application(db: Session, run: AgentRun, agent: BackgroundAgent, decision: dict) -> None:
    """Phase 2 — reviewer approved (or edited). Commit the masks/tags."""
    run.status = "applying"
    db.commit()
    result = await agent.apply(db, run, decision)
    _finalize_completed(db, run, result)


def mark_failed(db: Session, run: AgentRun, error: str) -> None:
    # The exception that got us here may have happened mid-flush (e.g. a
    # non-JSON-serializable value in output_json), which leaves the session's
    # transaction in a rolled-back state - committing again without first
    # rolling back here raises PendingRollbackError, which means the run never
    # actually gets marked "failed" at all and is stuck at its last status
    # forever. This is exactly the failure mode that must not itself fail.
    db.rollback()
    run.status = "failed"
    run.output_json = {"error": error}
    run.completed_at = datetime.utcnow()
    db.commit()


def mark_rejected(db: Session, run: AgentRun, notes: str | None) -> None:
    run.status = "rejected"
    run.output_json = {"phase": "rejected", "notes": notes}
    run.completed_at = datetime.utcnow()
    db.commit()
