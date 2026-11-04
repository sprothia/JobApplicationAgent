"""Functions the agent will call. Each takes options and returns data instead of printing."""

from apply import load_json
from config.constants import APPLICATIONS_FILE

HIDDEN_STATUSES = {"skipped", "closed"}


def find_jobs(
    role: str | None = None,
    company: str | None = None,
    status: str | None = None,
    only_new: bool = False,
    limit: int = 10,
) -> list[dict]:
    """Search prepared jobs in applications.json, newest first. Leaves out skipped and closed jobs.

    role: text the title must contain, e.g. "forward deployed" (case-insensitive).
    company: company name, e.g. "OpenAI" (case-insensitive).
    status: "ready" (every question answered) or "needs_input" (some left for you).
    only_new: only jobs first seen in the latest refresh.
    """
    matches = []
    for job_id, app in load_json(APPLICATIONS_FILE, {}).items():
        if app["status"] in HIDDEN_STATUSES:
            continue
        if role and role.lower() not in app["title"].lower():
            continue
        if company and company.lower() != app["company"].lower():
            continue
        if status and app["status"] != status:
            continue
        if only_new and not app.get("new"):
            continue
        matches.append(
            {
                "id": job_id,
                "company": app["company"],
                "title": app["title"].strip(),
                "location": app["location"],
                "status": app["status"],
                "unanswered": app["unanswered"],
                "posted_date": app.get("posted_date", ""),
                "url": app["url"],
            }
        )

    # ISO dates sort correctly as text. Jobs without a date go last.
    matches.sort(key=lambda job: job["posted_date"], reverse=True)
    return matches[:limit]
