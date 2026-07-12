"""A-02 Tagging — classifies a completed Sanitization run's output against the
controlled vocabulary. Conditional review: high-confidence in-vocabulary tags
auto-apply; low-confidence tags or new-term proposals pause for review."""

import time
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.agents.base import AgentFlag, AgentResult, AgentStep, BackgroundAgent, ReviewProposal
from app.agents.tagging import classifier
from app.config import get_settings
from app.models import AgentRun, DocumentMetadata, TagVocabulary
from app.storage.local_store import save_run_output
from app.tags import service as tags

settings = get_settings()


class TaggingAgent(BackgroundAgent):
    agent_id = "tagging"
    display_name = "Tagging"
    description = (
        "Classifies a sanitized document against the controlled tag vocabulary. Never sees raw input; "
        "auto-applies confident in-vocabulary tags and flags the rest for review."
    )
    tools = ["bedrock"]
    allowed_roles = ["admin", "km_governance", "km_reviewer", "practice_lead", "delivery"]

    def _load_source(self, db: Session, run: AgentRun):
        san_run_id = (run.input_json or {}).get("sanitization_run_id")
        if not san_run_id:
            raise ValueError("input.sanitization_run_id is required")
        san_run = db.get(AgentRun, uuid.UUID(str(san_run_id)))
        if san_run is None or san_run.agent_id != "sanitization":
            raise ValueError("sanitization_run_id must reference a Sanitization run")
        if san_run.status != "completed":
            raise ValueError(f"Sanitization run must be completed (is '{san_run.status}')")
        meta = db.query(DocumentMetadata).filter(DocumentMetadata.run_id == san_run.id).first()
        return san_run, meta

    async def detect(self, db: Session, run: AgentRun) -> ReviewProposal:
        steps: list[AgentStep] = []
        flags: list[AgentFlag] = []
        run.status = "tagging"
        db.commit()

        san_run, meta = self._load_source(db, run)
        summary = meta.sanitized_summary if meta else None
        hints = meta.metadata_json if meta else {}

        vocab = tags.approved_vocabulary(db)
        t = time.monotonic()
        resp = await classifier.classify(summary or "", hints, vocab)
        proposed = (resp.parsed or {}).get("tags", [])
        steps.append(AgentStep(order=1, name="classify (LLM)", tool="bedrock",
                               detail=f"{len(proposed)} tags proposed; {resp.input_tokens}+{resp.output_tokens} tok",
                               duration_ms=int((time.monotonic() - t) * 1000)))

        # Validate each against the vocabulary (deterministic hard gate).
        threshold = settings.TAG_CONFIDENCE_THRESHOLD
        resolved = []  # rows to display/apply
        needs_review = False
        for p in proposed:
            category, value = p.get("category"), (p.get("value") or "").strip().lower()
            confidence = float(p.get("confidence", 0.5))
            if not category or not value:
                continue
            vrow = tags.validate(db, category, value)
            if vrow is not None:
                if confidence >= threshold:
                    resolved.append({"category": category, "value": value, "confidence": confidence, "status": "applied"})
                else:
                    resolved.append({"category": category, "value": value, "confidence": confidence, "status": "flagged"})
                    flags.append(AgentFlag(message=f"Low confidence tag {category}={value} — confirm.", severity="warning"))
                    needs_review = True
            else:
                # Unknown term → governance proposal, never auto-applied.
                tags.propose_term(db, category, value, proposed_by=run.created_by)
                resolved.append({"category": category, "value": value, "confidence": confidence, "status": "proposed_new"})
                flags.append(AgentFlag(message=f"New term proposed: {category}={value} (pending governance).", severity="info"))
                needs_review = True
        db.commit()

        steps.append(AgentStep(order=2, name="validate", tool="tags.validate",
                               detail=f"{sum(1 for r in resolved if r['status'] == 'applied')} auto-applicable; "
                                      f"{sum(1 for r in resolved if r['status'] != 'applied')} need review"))

        summary_line = f"Tagging proposes {len(resolved)} tags ({'review needed' if needs_review else 'all auto-applied'})."
        return ReviewProposal(
            summary=summary_line,
            needs_review=needs_review,
            proposal={"sanitization_run_id": str(san_run.id), "tags": resolved},
            steps=steps,
            flags=flags,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            estimated_cost_usd=resp.estimated_cost_usd,
            working_status="tagging",
        )

    async def apply(self, db: Session, run: AgentRun, decision: dict) -> AgentResult:
        proposal = decision.get("proposal") or (run.output_json or {}).get("proposal") or {}
        edits = decision.get("edits") or {}
        removed = {(r.get("category"), r.get("value")) for r in edits.get("removed_tags", [])}

        # Apply only tags that are in the approved vocabulary and not reviewer-removed.
        resolved_rows = []
        applied = []
        for tg in proposal.get("tags", []):
            if (tg["category"], tg["value"]) in removed:
                continue
            vrow = tags.validate(db, tg["category"], tg["value"])
            if vrow is None:
                continue  # still-pending new terms are not applied to the doc
            status = "confirmed" if decision.get("decision") in ("approved", "edited") else "applied"
            resolved_rows.append((vrow, tg.get("confidence", 1.0), status))
            applied.append({"category": tg["category"], "value": tg["value"]})

        tags.apply_tags(db, run.id, resolved_rows)
        db.flush()

        run_id = str(run.id)
        pending_new = [t for t in proposal.get("tags", []) if t.get("status") == "proposed_new"]
        output = {
            "sanitization_run_id": proposal.get("sanitization_run_id"),
            "applied_tags": applied,
            "pending_new_terms": [{"category": t["category"], "value": t["value"]} for t in pending_new],
        }
        output_file = save_run_output(self.agent_id, run_id, {"run_id": run_id, "generated_at": datetime.utcnow().isoformat(), **output})

        return AgentResult(
            agent_id=self.agent_id,
            output=output,
            confidence=min([r[1] for r in resolved_rows], default=1.0),
            flags=[],
            steps=[AgentStep(order=1, name="apply tags", tool="tags.apply", detail=f"{len(applied)} tags written to run_tags")],
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            output_file_path=output_file,
        )
