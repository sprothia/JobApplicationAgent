"""Read and write the JSON files in data/ and profile/."""

import json
import os
from datetime import datetime, timezone

from config.constants import APPLICATIONS_FILE, SUBMITTED_FILE

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


OPEN_STATUSES = ("ready", "needs_input")  # jobs still to apply to


def record_jobs(job_ids: list[str], status: str) -> list[str]:
    """Mark open jobs "submitted" (moved to submitted.json) or "skipped" (permanent).
    Returns the ids that were open and got marked."""
    applications = load_json(APPLICATIONS_FILE, {})
    submitted = load_json(SUBMITTED_FILE, {})
    marked = [job_id for job_id in job_ids if applications.get(job_id, {}).get("status") in OPEN_STATUSES]
    for job_id in marked:
        if status == "submitted":
            app = applications.pop(job_id)
            app["status"] = "submitted"
            app["submitted_at"] = datetime.now(timezone.utc).isoformat()
            submitted[job_id] = app
        else:
            applications[job_id]["status"] = "skipped"
    save_json(SUBMITTED_FILE, submitted)
    save_json(APPLICATIONS_FILE, applications)
    return marked
