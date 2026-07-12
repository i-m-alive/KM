"""naviknow-mcp — the platform's MCP server, born with the Sanitization agent.

Runs as a stdio subprocess owned by the backend/worker. Exposes ONLY the
model-callable tools:
  - fs.read_document   (read a document's chunks)
  - review.create_task (file a proposal for human review)

Protected calls (masking_dictionary, source_registry, tags.validate, audit_log)
are deliberately NOT here — they are agent code the model can never reach.

Run from the backend/ directory so `app` is importable:
    python -m mcp_server.server
"""

import json
import uuid

from mcp.server.fastmcp import FastMCP

from app.db import SessionLocal
from app.documents.extract import extract_chunks
from app.models import AgentRun, ReviewItem, UploadedDocument

mcp = FastMCP("naviknow-mcp")


@mcp.tool()
def fs_read_document(document_id: str, start_chunk: int = 0, end_chunk: int | None = None) -> str:
    """Read a range of a document's structural chunks (pages / paragraph groups).

    Returns JSON: {document_id, filename, total_chunks, chunks:[{chunk_id,label,text}]}.
    Use start_chunk/end_chunk (inclusive) to page through large documents."""
    db = SessionLocal()
    try:
        doc = db.get(UploadedDocument, uuid.UUID(document_id))
        if doc is None:
            return json.dumps({"error": f"No document {document_id}"})
        chunks = extract_chunks(doc.stored_path, doc.content_type, doc.filename)
        end = len(chunks) - 1 if end_chunk is None else min(end_chunk, len(chunks) - 1)
        selected = [c for c in chunks if start_chunk <= c.chunk_id <= end]
        return json.dumps(
            {
                "document_id": document_id,
                "filename": doc.filename,
                "total_chunks": len(chunks),
                "chunks": [{"chunk_id": c.chunk_id, "label": c.label, "text": c.text} for c in selected],
            }
        )
    finally:
        db.close()


@mcp.tool()
def review_create_task(run_id: str, summary: str) -> str:
    """File a review task for a run so a human reviewer can approve/edit/reject.

    Records the reviewer-facing summary. The worker owns the run's status
    transition to awaiting_review; this records the review item."""
    db = SessionLocal()
    try:
        run = db.get(AgentRun, uuid.UUID(run_id))
        if run is None:
            return json.dumps({"error": f"No run {run_id}"})
        item = ReviewItem(run_id=run.id, notes=summary)
        db.add(item)
        db.commit()
        db.refresh(item)
        return json.dumps({"review_item_id": str(item.id), "run_id": run_id})
    finally:
        db.close()


if __name__ == "__main__":
    mcp.run()
