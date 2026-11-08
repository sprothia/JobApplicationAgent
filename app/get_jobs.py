"""Find matching jobs from three sources: Greenhouse, Ashby, and YC startups in SF.

  python app/get_jobs.py             Print every matching job.
  python app/get_jobs.py discover    Find the job boards of hiring YC startups and save them to
                                 YC_BOARDS_FILE. Takes a few minutes; rerun every week or so.
"""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from utils import http_client
from config.constants import (
    ASHBY_URL,
    GREENHOUSE_URL,
    LEVEL_KEYWORDS,
    NON_US_LOCATION_KEYWORDS,
    ROLE_KEYWORDS,
    EXCLUDE_KEYWORDS,
    SETTINGS_FILE,
    YC_BOARDS_FILE,
    YC_LOCATION,
    YC_MAX_TEAM_SIZE,
    YC_URL,
)


@dataclass
class Job:
    id: str
    company: str
    title: str
    location: str
    url: str
    posted_date: str
    source: str  # "greenhouse" or "ashby"
    board_token: str  # the company's Greenhouse/Ashby board name


def load_settings() -> dict:
    """The companies to track (read every run, since the agent can change them)."""
    with open(SETTINGS_FILE) as f:
        return json.load(f)


def save_settings(settings: dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")


# Boards that failed to load in the last get_jobs() run. Their jobs are missing from the results
# but probably still posted, so apply.py doesn't mark them closed.
failed_boards: set[str] = set()


# Source 1
def fetch_greenhouse(company: str, token: str) -> list[Job]:
    try:
        response = http_client.get(GREENHOUSE_URL.format(token=token))
    except requests.RequestException as e:
        print(f"Warning: failed to fetch {company} ({token}): {e}")
        failed_boards.add(token)
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
        failed_boards.add(token)
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


FETCHERS = {"greenhouse": fetch_greenhouse, "ashby": fetch_ashby}
PLATFORM_URLS = {"ashby": ASHBY_URL, "greenhouse": GREENHOUSE_URL}


# Source 3: YC startups in SF. Their boards are on Greenhouse or Ashby too, but we don't know
# their board names, so discover_yc_boards() finds them once and saves them to YC_BOARDS_FILE.
def fetch_yc() -> list[Job]:
    if not os.path.exists(YC_BOARDS_FILE):
        print(f"Warning: {YC_BOARDS_FILE} not found, run `python app/get_jobs.py discover` to add YC startups")
        return []
    with open(YC_BOARDS_FILE) as f:
        boards = json.load(f)
    jobs = []
    for board in boards:
        jobs.extend(FETCHERS[board["platform"]](board["company"], board["token"]))
    return jobs


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def candidate_tokens(company: dict) -> list[str]:
    """Board names to try: the YC slug, the slug without dashes, and the website's domain name."""
    tokens = [company["slug"], company["slug"].replace("-", "")]
    domain = urlparse(company.get("website") or "").netloc.removeprefix("www.")
    if domain:
        tokens.append(domain.split(".")[0])
    return list(dict.fromkeys(t for t in tokens if t))


def board_jobs(platform: str, token: str) -> list[dict]:
    try:
        data = http_client.get(PLATFORM_URLS[platform].format(token=token)).json()
    except (requests.RequestException, ValueError):
        return []
    return data.get("jobs", [])


def belongs_to(company: dict, jobs: list[dict]) -> bool:
    """Guard against a board with the same name that belongs to a different company."""
    name = normalize(company["name"])
    text = " ".join(
        str(job.get("company_name", "")) + str(job.get("descriptionPlain", "")) for job in jobs[:5]
    )
    # Greenhouse's list endpoint has no descriptions, only company_name.
    return name in normalize(text)


def find_board(company: dict) -> dict | None:
    for token in candidate_tokens(company):
        for platform in PLATFORM_URLS:
            jobs = board_jobs(platform, token)
            if jobs and belongs_to(company, jobs):
                return {"company": company["name"], "platform": platform, "token": token}
    return None


def discover_yc_boards():
    settings = load_settings()
    already_tracked = {*settings["greenhouse"].values(), *settings["ashby"].values()}
    companies = [
        c
        for c in requests.get(YC_URL, timeout=60).json()
        if YC_LOCATION in (c.get("all_locations") or "")
        and (c.get("team_size") or 0) <= YC_MAX_TEAM_SIZE
        and not already_tracked.intersection(candidate_tokens(c))
    ]
    print(f"Checking {len(companies)} YC companies...")

    # Kept low so Ashby doesn't rate-limit us.
    with ThreadPoolExecutor(max_workers=6) as pool:
        found = [b for b in pool.map(find_board, companies) if b]
    # Two YC companies can share a name (e.g. "Conduit") and match the same board; keep it once.
    boards = list({(b["platform"], b["token"]): b for b in found}.values())

    with open(YC_BOARDS_FILE, "w") as f:
        json.dump(boards, f, indent=2)
    print(f"Found {len(boards)} job boards, saved to {YC_BOARDS_FILE}")


# Filters
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


def get_jobs() -> list[Job]:
    failed_boards.clear()
    settings = load_settings()
    jobs = []
    for company, token in settings["greenhouse"].items():
        jobs.extend(job for job in fetch_greenhouse(company, token) if is_relevant(job))
    for company, token in settings["ashby"].items():
        jobs.extend(job for job in fetch_ashby(company, token) if is_relevant(job))
    jobs.extend(job for job in fetch_yc() if is_startup_relevant(job))
    ignored = {name.lower() for name in settings["ignored_companies"]}
    return [job for job in jobs if not is_outside_us(job) and job.company.lower() not in ignored]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "discover":
        discover_yc_boards()
    else:
        jobs = get_jobs()
        for job in jobs:
            print(f"{job.company} | {job.title} | {job.location} | {job.url}")
        print(f"\nTotal: {len(jobs)} jobs")
