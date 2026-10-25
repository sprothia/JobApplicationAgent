"""Find the Greenhouse/Ashby job board for each hiring YC startup in SF.

Writes YC_BOARDS_FILE, which get_jobs.py reads. Rerun every week or so to pick up new startups.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
import http_client
from constants import (
    ASHBY_COMPANIES,
    ASHBY_URL,
    GREENHOUSE_COMPANIES,
    GREENHOUSE_URL,
    YC_BOARDS_FILE,
    YC_LOCATION,
    YC_MAX_TEAM_SIZE,
    YC_URL,
)

PLATFORM_URLS = {"ashby": ASHBY_URL, "greenhouse": GREENHOUSE_URL}
ALREADY_TRACKED = {*GREENHOUSE_COMPANIES.values(), *ASHBY_COMPANIES.values()}


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


def main():
    companies = [
        c
        for c in requests.get(YC_URL, timeout=60).json()
        if YC_LOCATION in (c.get("all_locations") or "")
        and (c.get("team_size") or 0) <= YC_MAX_TEAM_SIZE
        and not ALREADY_TRACKED.intersection(candidate_tokens(c))
    ]
    print(f"Checking {len(companies)} YC companies...")

    # Kept low so Ashby doesn't rate-limit us.
    with ThreadPoolExecutor(max_workers=6) as pool:
        boards = [b for b in pool.map(find_board, companies) if b]

    with open(YC_BOARDS_FILE, "w") as f:
        json.dump(boards, f, indent=2)
    print(f"Found {len(boards)} job boards, saved to {YC_BOARDS_FILE}")


if __name__ == "__main__":
    main()
