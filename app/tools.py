"""Functions the agent will call. Each takes options and returns data instead of printing."""

from datetime import datetime, timedelta, timezone

import apply
from apply import load_json
from config.constants import APPLICATIONS_FILE, PROFILE_FILE, SUBMITTED_FILE
from get_jobs import failed_boards

HIDDEN_STATUSES = {"skipped", "closed"}


def summarize(job_id: str, app: dict) -> dict:
    """The parts of a job the agent needs (applications.json also holds every form answer)."""
    return {
        "id": job_id,
        "company": app["company"],
        "title": app["title"].strip(),
        "location": app["location"],
        "status": app["status"],
        "unanswered": app["unanswered"],
        "posted_date": app.get("posted_date", ""),
        "url": app["url"],
    }


def search_open_jobs(
    role: str | None = None,
    company: str | None = None,
    status: str | None = None,
    only_new: bool = False,
    limit: int = 10,
) -> list[dict]:
    """Search all open jobs he hasn't applied to or skipped yet, as of the last check_for_new_jobs (saved in
    applications.json). Instant. Doesn't look for postings newer than that check. Newest first.

    role: text the title must contain, e.g. "forward deployed" (case-insensitive).
    company: company name, e.g. "OpenAI" (case-insensitive).
    status: "ready" (every question answered) or "needs_input" (some left for him).
    only_new: only jobs that were new in the latest check_for_new_jobs.
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
        matches.append(summarize(job_id, app))

    # ISO dates sort correctly as text. Jobs without a date go last.
    matches.sort(key=lambda job: job["posted_date"], reverse=True)
    return matches[:limit]


def check_for_new_jobs() -> dict:
    """Go to every tracked company's job board, find postings that are new since the last check, save them,
    and prepare their answers. Takes 1-2 minutes.

    Returns the totals per status, the jobs new since the last check, the jobs taken down since
    then, and any job boards that failed to load.
    """
    result = apply.refresh_jobs(load_json(PROFILE_FILE, None))
    return {
        "totals": result["totals"],
        "new": [
            {key: app[key] for key in ("company", "title", "location", "status", "unanswered")}
            for app in result["new"]
        ],
        "closed": [{"company": app["company"], "title": app["title"].strip()} for app in result["closed"]],
        "failed_boards": sorted(failed_boards),
    }


def list_submitted(company: str | None = None, days: int | None = None, limit: int = 20) -> list[dict]:
    """Jobs he has applied to (in submitted.json), most recent first.

    company: only this company (case-insensitive).
    days: only applications from the last this many days, e.g. 7 for this week.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    matches = []
    for app in load_json(SUBMITTED_FILE, {}).values():
        if company and company.lower() != app["company"].lower():
            continue
        if since and datetime.fromisoformat(app["submitted_at"]) < since:
            continue
        matches.append(
            {
                "company": app["company"],
                "title": app["title"].strip(),
                "location": app["location"],
                "submitted_at": app["submitted_at"],
            }
        )
    matches.sort(key=lambda app: app["submitted_at"], reverse=True)
    return matches[:limit]
