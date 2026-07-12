import json
import os
from typing import Any

from app.config import get_settings

settings = get_settings()


def save_run_output(agent_id: str, run_id: str, data: dict[str, Any]) -> str:
    """Writes a run's output to backend/outputs/<agent_id>/<run_id>.json and returns the path."""
    agent_dir = os.path.join(settings.OUTPUTS_DIR, agent_id)
    os.makedirs(agent_dir, exist_ok=True)

    file_path = os.path.join(agent_dir, f"{run_id}.json")
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    return file_path


def save_masked_document(run_id: str, original_filename: str, masked_chunks: list[dict]) -> str:
    """Writes the sanitized document as a readable .txt under
    backend/outputs/sanitization/, named after the original file."""
    out_dir = os.path.join(settings.OUTPUTS_DIR, "sanitization")
    os.makedirs(out_dir, exist_ok=True)

    stem = os.path.splitext(os.path.basename(original_filename))[0]
    file_path = os.path.join(out_dir, f"{run_id}__{stem}.sanitized.txt")
    with open(file_path, "w") as f:
        for c in masked_chunks:
            f.write(f"=== {c.get('label', 'chunk')} ===\n")
            f.write(c.get("text", ""))
            f.write("\n\n")
    return file_path
