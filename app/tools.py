"""Functions the agent will call. Each takes options and returns data instead of printing."""

import json
import re
from datetime import datetime, timedelta, timezone

import apply
from apply import load_json
from config.constants import APPLICATIONS_FILE, PROFILE_FILE, SUBMITTED_FILE
from config.constants import YC_BOARDS_FILE
from get_jobs import PLATFORM_URLS, board_jobs, failed_boards, load_settings, save_settings

HIDDEN_STATUSES = {"skipped", "closed"}


def ask_in_chat(question: str) -> str:
    """How apply_to_jobs asks him things: show the question and wait for his reply."""
    print("\nAgent: " + question.strip())
    return input("You: ")


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


def mentions_any(location: str, places: list[str]) -> bool:
    """Whole words, case-insensitive, so "SF" doesn't match inside another word."""
    return any(re.search(rf"\b{re.escape(place)}\b", location, re.IGNORECASE) for place in places)


def posted_since(posted_date: str, days: int) -> bool:
    if not posted_date:
        return False
    posted = datetime.fromisoformat(posted_date.replace("Z", "+00:00"))
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return posted >= datetime.now(timezone.utc) - timedelta(days=days)


def search_open_jobs(
    role: str | None = None,
    company: str | None = None,
    status: str | None = None,
    only_new: bool = False,
    locations: list[str] | None = None,
    exclude_locations: list[str] | None = None,
    posted_within_days: int | None = None,
    limit: int = 10,
) -> list[dict]:
    """Search all open jobs he hasn't applied to or skipped yet, as of the last check_for_new_jobs (saved in
    applications.json). Instant. Doesn't look for postings newer than that check. Newest first.

    role: text the title must contain, e.g. "forward deployed" (case-insensitive).
    company: company name, e.g. "OpenAI" (case-insensitive).
    status: "ready" (every question answered) or "needs_input" (some left for him).
    only_new: only jobs that were new in the latest check_for_new_jobs.
    locations: keep jobs whose location mentions any of these. Locations are written many ways, so pass
        every spelling, e.g. ["San Francisco", "SF"] or ["New York", "NYC", "NY"], or ["Remote"].
    exclude_locations: drop jobs whose location mentions any of these (same spelling advice).
    posted_within_days: only jobs posted in the last this many days, e.g. 7 for this week. Jobs with
        no saved posting date are left out when this is set.
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
        if locations and not mentions_any(app["location"], locations):
            continue
        if exclude_locations and mentions_any(app["location"], exclude_locations):
            continue
        if posted_within_days and not posted_since(app.get("posted_date", ""), posted_within_days):
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


def apply_to_jobs(job_ids: list[str]) -> list[dict]:
    """Apply to open jobs by id (ids come from search_open_jobs). For each job this opens the form in a
    browser, fills in everything from his profile, asks him any question the profile can't answer, and
    waits while he reviews and clicks Submit himself. Asking happens inside this tool, so don't ask
    him the questions beforehand.

    Returns each job's result: "submitted", "skipped", or "not done" (he stopped before it).
    """
    applications = load_json(APPLICATIONS_FILE, {})
    open_ids = [job_id for job_id in job_ids if applications.get(job_id, {}).get("status") in apply.OPEN_STATUSES]
    results = apply.fill_jobs(load_json(PROFILE_FILE, None), open_ids, ask=ask_in_chat) if open_ids else []
    results += [{"id": job_id, "result": "not an open job id"} for job_id in job_ids if job_id not in open_ids]
    return results


def list_companies() -> dict:
    """The companies being tracked: hand-picked ones by platform, how many YC startups, and ignored ones."""
    settings = load_settings()
    with open(YC_BOARDS_FILE) as f:
        yc = json.load(f)
    return {
        "greenhouse": sorted(settings["greenhouse"]),
        "ashby": sorted(settings["ashby"]),
        "yc_startups": len(yc),
        "ignored": settings["ignored_companies"],
    }


def add_company(name: str, board_token: str | None = None) -> str:
    """Start tracking a company. Finds its Greenhouse or Ashby job board by trying board names made from
    the company name, or uses board_token if given (the name in its board URL, e.g. "openai" in
    jobs.ashbyhq.com/openai). Only Greenhouse and Ashby are supported. New jobs show up on the next
    check_for_new_jobs.
    """
    settings = load_settings()
    simple = re.sub(r"[^a-z0-9]", "", name.lower())
    tokens = [board_token] if board_token else list(dict.fromkeys([simple, re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")]))
    for token in tokens:
        for platform in PLATFORM_URLS:
            jobs = board_jobs(platform, token)
            if jobs:
                settings[platform][name] = token
                settings["ignored_companies"] = [c for c in settings["ignored_companies"] if c.lower() != name.lower()]
                save_settings(settings)
                return f"Added {name}: {platform} board '{token}' with {len(jobs)} open jobs (all roles, before filters)."
    return f"No Greenhouse or Ashby board with open jobs found for {name} (tried {', '.join(tokens)}). It may use another platform, or ask him for the board URL."


def remove_company(name: str) -> str:
    """Stop tracking a company, hand-picked or YC startup. Its open jobs get marked closed on the next
    check_for_new_jobs. add_company undoes this."""
    settings = load_settings()
    removed = [
        platform for platform in PLATFORM_URLS
        if settings[platform].pop(next((c for c in settings[platform] if c.lower() == name.lower()), None), None)
    ]
    if name.lower() not in (c.lower() for c in settings["ignored_companies"]):
        settings["ignored_companies"].append(name)
    save_settings(settings)
    return f"Stopped tracking {name}" + (f" (removed from {', '.join(removed)})." if removed else " (it will be ignored if it shows up, e.g. as a YC startup).")
