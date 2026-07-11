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
