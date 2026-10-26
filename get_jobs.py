import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
import requests
import http_client
from constants import (
    ASHBY_COMPANIES,
    ASHBY_URL,
    GREENHOUSE_COMPANIES,
    GREENHOUSE_URL,
    LEVEL_KEYWORDS,
    NON_US_LOCATION_KEYWORDS,
    ROLE_KEYWORDS,
    EXCLUDE_KEYWORDS,
    SIMPLIFY_CATEGORIES,
    SIMPLIFY_COMPANIES,
    SIMPLIFY_URL,
    YC_BOARDS_FILE,
)


@dataclass
class Job:
    id: str
    company: str
    title: str
    location: str
    url: str
    posted_date: str
    source: str  # "greenhouse", "ashby" or "simplify"
    board_token: str  # Greenhouse/Ashby board name; empty for SimplifyJobs

# Source 1
def fetch_greenhouse(company: str, token: str) -> list[Job]:
    try:
        response = http_client.get(GREENHOUSE_URL.format(token=token))
    except requests.RequestException as e:
        print(f"Warning: failed to fetch {company} ({token}): {e}")
        return []

    return [
        Job(
            id=str(job["id"]),
            company=company,
            title=job["title"],
            location=job["location"]["name"],
            url=job["absolute_url"],
            posted_date=job.get("first_published", ""),
            source="greenhouse",
            board_token=token,
        )
        for job in response.json()["jobs"]
    ]


# Source 2
def fetch_ashby(company: str, token: str) -> list[Job]:
    try:
        response = http_client.get(ASHBY_URL.format(token=token))
    except requests.RequestException as e:
        print(f"Warning: failed to fetch {company} ({token}): {e}")
        return []

    return [
        Job(
            id=job["id"],
            company=company,
            title=job["title"],
            location=job.get("location", ""),
            url=job["jobUrl"],
            posted_date=job.get("publishedAt", ""),
            source="ashby",
            board_token=token,
        )
        for job in response.json()["jobs"]
    ]


# Source 3: listings are already new grad, so they only need has_role().
def fetch_simplify() -> list[Job]:
    try:
        response = http_client.get(SIMPLIFY_URL, timeout=60)
    except requests.RequestException as e:
        print(f"Warning: failed to fetch SimplifyJobs: {e}")
        return []

    return [
        Job(
            id=job["id"],
            company=job["company_name"],
            title=job["title"],
            location="; ".join(job.get("locations", [])),
            url=job["url"],
            posted_date=datetime.fromtimestamp(job["date_posted"], timezone.utc).isoformat(),
            source="simplify",
            board_token="",
        )
        for job in response.json()
        if job.get("active")
        and job.get("is_visible", True)
        and job["company_name"] in SIMPLIFY_COMPANIES
        and job.get("category") in SIMPLIFY_CATEGORIES
    ]


def is_outside_us(job: Job) -> bool:
    # Whole words, so "uk" doesn't match inside another word.
    location = job.location.lower()
    return any(re.search(rf"\b{re.escape(k)}\b", location) for k in NON_US_LOCATION_KEYWORDS)


def has_role(job: Job) -> bool:
    return any(k in job.title.lower() for k in ROLE_KEYWORDS)


def is_relevant(job: Job) -> bool:
    return any(k in job.title.lower() for k in LEVEL_KEYWORDS) and has_role(job)


def is_excluded(job: Job) -> bool:
    # "Member of Technical Staff" is a regular role, not a staff-level one.
    title = job.title.lower().replace("member of technical staff", "")
    return any(k in title for k in EXCLUDE_KEYWORDS)


def is_startup_relevant(job: Job) -> bool:
    """Startups rarely put a level in the title, so keep any matching role that isn't senior or an internship."""
    return is_relevant(job) or (has_role(job) and not is_excluded(job))


FETCHERS = {"greenhouse": fetch_greenhouse, "ashby": fetch_ashby}


def load_yc_boards() -> list[dict]:
    if not os.path.exists(YC_BOARDS_FILE):
        print(f"Warning: {YC_BOARDS_FILE} not found, run discover_yc.py to add YC startups")
        return []
    with open(YC_BOARDS_FILE) as f:
        return json.load(f)


def get_jobs() -> list[Job]:
    jobs = []
    for company, token in GREENHOUSE_COMPANIES.items():
        jobs.extend(job for job in fetch_greenhouse(company, token) if is_relevant(job))
    for company, token in ASHBY_COMPANIES.items():
        jobs.extend(job for job in fetch_ashby(company, token) if is_relevant(job))
    jobs.extend(job for job in fetch_simplify() if has_role(job))
    for board in load_yc_boards():
        fetch = FETCHERS[board["platform"]]
        jobs.extend(job for job in fetch(board["company"], board["token"]) if is_startup_relevant(job))
    return [job for job in jobs if not is_outside_us(job)]


if __name__ == "__main__":
    jobs = get_jobs()
    for job in jobs:
        print(f"{job.company} | {job.title} | {job.location} | {job.url}")
    print(f"\nTotal: {len(jobs)} jobs")
