from dataclasses import dataclass
from datetime import datetime, timezone
import requests
from constants import (
    ASHBY_COMPANIES,
    ASHBY_URL,
    GREENHOUSE_COMPANIES,
    GREENHOUSE_URL,
    LEVER_COMPANIES,
    LEVER_URL,
    LEVEL_KEYWORDS,
    ROLE_KEYWORDS,
    SIMPLIFY_CATEGORIES,
    SIMPLIFY_COMPANIES,
    SIMPLIFY_URL,
)


@dataclass
class Job:
    id: str
    company: str
    title: str
    location: str
    url: str
    posted_date: str

# Source 1
def fetch_greenhouse(company: str, token: str) -> list[Job]:
    try:
        response = requests.get(GREENHOUSE_URL.format(token=token), timeout=15)
        response.raise_for_status()
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
        )
        for job in response.json()["jobs"]
    ]


# Source 2
def fetch_ashby(company: str, token: str) -> list[Job]:
    try:
        response = requests.get(ASHBY_URL.format(token=token), timeout=15)
        response.raise_for_status()
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
        )
        for job in response.json()["jobs"]
    ]


# Source 3
def fetch_lever(company: str, token: str) -> list[Job]:
    try:
        response = requests.get(LEVER_URL.format(token=token), timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Warning: failed to fetch {company} ({token}): {e}")
        return []

    return [
        Job(
            id=job["id"],
            company=company,
            title=job["text"],
            location=job["categories"].get("location", ""),
            url=job["hostedUrl"],
            # Lever gives createdAt in milliseconds since epoch
            posted_date=datetime.fromtimestamp(job["createdAt"] / 1000, timezone.utc).isoformat(),
        )
        for job in response.json()
    ]


# Source 4: listings are already new grad, so they only need has_role().
def fetch_simplify() -> list[Job]:
    try:
        response = requests.get(SIMPLIFY_URL, timeout=60)
        response.raise_for_status()
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
        )
        for job in response.json()
        if job.get("active")
        and job.get("is_visible", True)
        and job["company_name"] in SIMPLIFY_COMPANIES
        and job.get("category") in SIMPLIFY_CATEGORIES
    ]


def has_role(job: Job) -> bool:
    return any(k in job.title.lower() for k in ROLE_KEYWORDS)


def is_relevant(job: Job) -> bool:
    return any(k in job.title.lower() for k in LEVEL_KEYWORDS) and has_role(job)


def get_jobs() -> list[Job]:
    jobs = []
    for company, token in GREENHOUSE_COMPANIES.items():
        jobs.extend(job for job in fetch_greenhouse(company, token) if is_relevant(job))
    for company, token in ASHBY_COMPANIES.items():
        jobs.extend(job for job in fetch_ashby(company, token) if is_relevant(job))
    for company, token in LEVER_COMPANIES.items():
        jobs.extend(job for job in fetch_lever(company, token) if is_relevant(job))
    jobs.extend(job for job in fetch_simplify() if has_role(job))
    return jobs


if __name__ == "__main__":
    jobs = get_jobs()
    for job in jobs:
        print(f"{job.company} | {job.title} | {job.location} | {job.url}")
    print(f"\nTotal: {len(jobs)} jobs")
