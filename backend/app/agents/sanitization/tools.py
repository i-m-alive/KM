"""Model-callable tools for the Sanitization Detector, as plain in-process
Python functions passed directly into the Bedrock Converse tool-use loop —
no MCP server/subprocess/JSON-RPC round trip for a single in-house consumer.

Deliberately contains ONLY the one tool the model is allowed to call
(fs_read_document). "Protected calls" (masking dictionary, tags.validate,
review filing, audit log) live in ordinary agent code and are never put in
a TOOLS dict — the model can't reach them, not just told not to. This is the
same guardrail the old MCP server enforced by simply never registering them
as @mcp.tool(), reproduced here by the same omission.
"""

import json
import uuid

from app.db import SessionLocal
from app.documents.extract import extract_chunks
from app.models import UploadedDocument

FS_READ_DOCUMENT = "fs_read_document"


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


# Bedrock Converse toolSpec — the exact shape converse_with_tools expects,
# built here instead of derived from an MCP list_tools() round trip.
FS_READ_DOCUMENT_SPEC = {
    "toolSpec": {
        "name": FS_READ_DOCUMENT,
        "description": fs_read_document.__doc__,
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "start_chunk": {"type": "integer"},
                    "end_chunk": {"type": ["integer", "null"]},
                },
                "required": ["document_id"],
            }
        },
    }
}


async def sanitization_tool_executor(name: str, arguments: dict) -> str:
    """The only tool_executor the Detector's tool-use loop is given —
    fs_read_document is the sole callable name, by construction."""
    if name == FS_READ_DOCUMENT:
        return fs_read_document(
            arguments.get("document_id"),
            arguments.get("start_chunk", 0),
            arguments.get("end_chunk"),
        )
    return json.dumps({"error": f"tool {name} not available"})
