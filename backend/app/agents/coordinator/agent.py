"""A-04 Coordinator — no content work of its own. Sequences Sanitization into
Tagging so a user reviews once and the rest of the pipeline runs automatically,
instead of manually clicking "Run Tagging" after every Sanitization approval.

Deliberately minimal against the full design-doc spec: no eligibility-rule
engine, no persisted pipeline-run record distinct from the two agent runs it
creates. What actually delivers "review once, everything else happens
automatically" is the auto-chain hook in app.runs.background
(_maybe_auto_chain_to_tagging) - the Coordinator's only job is to start
Sanitization with the auto_chain_to marker set. Reused, not reinvented: the
child Sanitization run goes through run_detection() exactly as if the worker
had picked it up directly, so it gets every existing guardrail (review gate,
5-channel verification, confidence threshold) with zero duplicated logic.

Runs as an ordinary BackgroundAgent so it's processed by the SAME worker poll
loop as Sanitization/Tagging - no new infrastructure, no waiting/polling
inside a single call. needs_review is always False: there is nothing for a
human to approve at the orchestration level itself - the human review that
matters happens on the child Sanitization run, same as if it had been started
directly from the Documents page.
"""

import time
import uuid

from sqlalchemy.orm import Session

from app.agents.base import AgentFlag, AgentResult, AgentStep, BackgroundAgent, ReviewProposal
from app.models import AgentRun, UploadedDocument


class CoordinatorAgent(BackgroundAgent):
    agent_id = "coordinator"
    display_name = "Coordinator"
    description = (
        "Chains Sanitization into Tagging automatically: starts Sanitization on a document, "
        "and once that run is reviewed/approved and verifies clean, auto-starts Tagging on it - "
        "no manual 'Run Tagging' click needed. Does no content work itself."
    )
    tools: list[str] = []
    allowed_roles = ["admin", "km_governance", "km_reviewer", "practice_lead", "delivery"]

    async def detect(self, db: Session, run: AgentRun) -> ReviewProposal:
        # Deferred imports: avoids a module-load-time cycle with the registry
        # (which lazily imports this module) and keeps this file free of any
        # dependency the plain orchestration job doesn't need.
        from app.agents.registry import get_agent
        from app.runs.background import mark_failed, run_detection

        steps: list[AgentStep] = []
        flags: list[AgentFlag] = []

        document_id = (run.input_json or {}).get("document_id")
        if not document_id:
            raise ValueError("input.document_id is required")
        doc = db.get(UploadedDocument, uuid.UUID(str(document_id)))
        if doc is None:
            raise ValueError(f"No document {document_id}")

        sanitization_agent = get_agent("sanitization")
        if sanitization_agent is None:
            raise ValueError("Sanitization agent is not available")

        t = time.monotonic()
        sanitization_run = AgentRun(
            agent_id="sanitization",
            status="pending",
            # The one marker _maybe_auto_chain_to_tagging looks for - this is
            # the ENTIRE mechanism that makes "review once, Tagging follows
            # automatically" work; everything else is Sanitization's own,
            # unmodified pipeline.
            input_json={"document_id": document_id, "auto_chain_to": "tagging"},
            created_by=run.created_by,
        )
        db.add(sanitization_run)
        db.commit()
        db.refresh(sanitization_run)

        try:
            await run_detection(db, sanitization_run, sanitization_agent)
        except Exception as exc:
            mark_failed(db, sanitization_run, str(exc))
            raise ValueError(f"Sanitization failed to start for '{doc.filename}': {exc}") from exc

        steps.append(AgentStep(
            order=1, name="start Sanitization", tool=None,
            detail=f"Sanitization run {sanitization_run.id} on '{doc.filename}' is now '{sanitization_run.status}'.",
            duration_ms=int((time.monotonic() - t) * 1000),
        ))
        if sanitization_run.status == "awaiting_review":
            flags.append(AgentFlag(
                message="Sanitization needs human review before this pipeline can continue. "
                        "Tagging will start automatically once it's approved and verifies clean.",
                severity="info",
            ))

        summary = (
            f"Started Sanitization on '{doc.filename}' (run {sanitization_run.id}); "
            "Tagging will auto-start once it's approved and verified clean."
        )
        return ReviewProposal(
            summary=summary,
            needs_review=False,  # nothing to approve at this level - see module docstring
            proposal={"document_id": document_id, "sanitization_run_id": str(sanitization_run.id)},
            steps=steps,
            flags=flags,
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            working_status="orchestrating",
        )

    async def apply(self, db: Session, run: AgentRun, decision: dict) -> AgentResult:
        # The real work already happened in detect() (the child Sanitization
        # run was created and started); this just finalizes the Coordinator's
        # own run record so it shows up as "completed" in Run history.
        proposal = decision.get("proposal") or (run.output_json or {}).get("proposal") or {}
        return AgentResult(
            agent_id=self.agent_id,
            output=proposal,
            confidence=1.0,
            flags=[],
            steps=[AgentStep(order=1, name="orchestration complete", tool=None, detail="Sanitization started; see its run for progress.")],
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            output_file_path=None,
        )
