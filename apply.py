"""Prepare and fill Greenhouse applications from profile.json.

  python apply.py                 Prepare: for each Greenhouse job from get_jobs(), fetch its form and
                                  answer every question from the profile. Questions the profile can't
                                  answer are left blank: if any are required, the job is marked
                                  'needs_input' and they're listed. Results go to applications.json.
  python apply.py fill [company]  Fill: open each prepared job in a browser and fill it in. You review,
                                  answer anything left blank and click Submit yourself. Jobs you mark
                                  submitted move from applications.json to submitted.json.
"""

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone

import requests
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

import http_client
from get_jobs import Job, get_jobs

PROFILE_FILE = "profile.json"
APPLICATIONS_FILE = "applications.json"  # jobs still to apply to
SUBMITTED_FILE = "submitted.json"  # jobs you've applied to
QUESTIONS_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true"

# Greenhouse's standard fields, filled from profile["basics"] (resume comes from profile["resume_path"]).
BASIC_FIELDS = {"first_name", "last_name", "preferred_name", "email", "phone", "location"}
# Alternatives to an upload (paste resume as text) and fields the browser fills itself (lat/long).
IGNORED_FIELDS = {"resume_text", "cover_letter_text", "latitude", "longitude"}


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path: str, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def fetch_questions(job: Job) -> list[dict]:
    """All questions on the form, normalized to {label, required, fields: [{name, type, options}]}."""
    data = http_client.get(QUESTIONS_URL.format(token=job.board_token, id=job.id)).json()

    questions = list(data.get("questions") or []) + list(data.get("location_questions") or [])
    for section in data.get("compliance") or []:
        questions.extend(section["questions"])

    normalized = [
        {
            "label": q["label"],
            "required": q["required"],
            "fields": [
                {"name": f["name"], "type": f["type"], "options": [o["label"] for o in f.get("values", [])]}
                for f in q["fields"]
            ],
        }
        for q in questions
    ]

    # The page asks "Are you Hispanic/Latino?" before showing the Race dropdown, but the API leaves it out.
    for q in normalized:
        if any(f["name"] == "race" for f in q["fields"]):
            q["fields"].insert(
                0,
                {
                    "name": "hispanic_ethnicity",
                    "type": "multi_value_single_select",
                    "options": ["Yes", "No", "Decline To Self Identify"],
                },
            )

    # Newer demographic survey: each question is a single field with answer_options,
    # and its element id on the page is the question's id.
    for q in (data.get("demographic_questions") or {}).get("questions", []):
        normalized.append(
            {
                "label": q["label"],
                "required": q["required"],
                "fields": [
                    {
                        "name": str(q["id"]),
                        "type": q["type"],
                        "options": [o["label"] for o in q["answer_options"]],
                    }
                ],
            }
        )
    return normalized


def find_answer(label: str, profile: dict) -> list[str]:
    """The first answer-bank entry with a keyword in the label wins.

    An answer can be one string or a list of equivalent phrasings (forms word dropdown options
    differently). Returns [] if no entry matches or its answer is blank.
    """
    label = label.lower()
    for entry in profile["answers"]:
        if any(k.lower() in label for k in entry["keywords"]):
            answer = entry["answer"]
            phrasings = answer if isinstance(answer, list) else [answer]
            return [p.strip() for p in phrasings if p.strip()]
    return []


def pick_option(phrasings: list[str], options: list[str]) -> str:
    """Match an answer to a dropdown option, trying the strictest match first:
    an exact match, then an option that starts with a phrasing, then one that contains it.
    If several options match equally (e.g. "Asian" in both "East Asian" and "South Asian"),
    don't guess: return "" so the question is left for the user."""
    patterns = [
        lambda p, o: o == p,
        lambda p, o: re.match(rf"{re.escape(p)}\b", o),
        lambda p, o: re.search(rf"\b{re.escape(p)}\b", o),
    ]
    for matches in patterns:
        for phrasing in phrasings:
            hits = [option for option in options if matches(phrasing.lower(), option.lower().strip())]
            if len(hits) == 1:
                return hits[0]
            if hits:
                return ""
    return ""


def answer_field(field: dict, label: str, profile: dict) -> str:
    name, field_type = field["name"], field["type"]
    if name == "resume":
        return profile["resume_path"]
    if name in BASIC_FIELDS:
        return profile["basics"].get(name, "").strip()
    if field_type == "input_file":  # cover letter, transcript, ...: we have nothing to upload
        return ""

    # EEO questions hold several fields under one label (e.g. "Race" has hispanic_ethnicity and race),
    # so include the field name in the text we match keywords against.
    if not name.startswith("question_"):
        label = f"{name.replace('_', ' ')} {label}"
    phrasings = find_answer(label, profile)
    if not phrasings:
        return ""
    if field_type.startswith("multi_value"):
        return pick_option(phrasings, field["options"])
    return phrasings[0]  # text fields use the first phrasing


def prepare(job: Job, profile: dict) -> dict:
    """Answer every question on the job's form. Status is 'ready', or 'needs_input' if a required one is blank."""
    answers, unanswered = [], []
    for question in fetch_questions(job):
        values = {
            field["name"]: answer_field(field, question["label"], profile)
            for field in question["fields"]
            if field["name"] not in IGNORED_FIELDS and field["type"] != "input_hidden"
        }
        filled = {name: value for name, value in values.items() if value}
        if filled:
            answers.append({"label": question["label"], "values": filled})
        elif question["required"] and values:
            unanswered.append(question["label"])

    return {
        "company": job.company,
        "title": job.title,
        "location": job.location,
        # The embed URL shows Greenhouse's form directly; the normal job URL redirects to the
        # company's own careers site for some companies (Pinterest, Waymo, Nuro, Roblox, ...).
        "url": f"https://job-boards.greenhouse.io/embed/job_app?for={job.board_token}&token={job.id}",
        "status": "needs_input" if unanswered else "ready",
        "unanswered": unanswered,
        "answers": answers,
    }


def prepare_all(profile: dict):
    applications = load_json(APPLICATIONS_FILE, {})
    submitted = load_json(SUBMITTED_FILE, {})
    jobs = [
        job
        for job in get_jobs()
        if job.source == "greenhouse"
        and job.id not in submitted
        and applications.get(job.id, {}).get("status") != "skipped"  # skips are permanent
    ]
    print(f"Preparing {len(jobs)} Greenhouse jobs...")

    for job in jobs:
        try:
            applications[job.id] = prepare(job, profile)
        except requests.RequestException as e:
            print(f"Warning: failed to fetch form for {job.company} - {job.title}: {e}")

    save_json(APPLICATIONS_FILE, applications)

    statuses = Counter(app["status"] for app in applications.values())
    print(
        f"\nReady: {statuses['ready']}   Needs input: {statuses['needs_input']}   "
        f"Skipped: {statuses['skipped']}   Submitted (in {SUBMITTED_FILE}): {len(submitted)}"
    )

    print("\nReady (every required question answered):")
    for app in applications.values():
        if app["status"] == "ready":
            print(f"  {app['company']} | {app['title']} | {app['location']}")

    print("\nNeeds input (required questions left blank for you):")
    for app in applications.values():
        if app["status"] == "needs_input":
            print(f"  {app['company']} | {app['title']} | {app['location']}")
            for label in app["unanswered"]:
                print(f"      - {label.strip()[:100]}")


# ---------- Fill ----------

def field_locator(page: Page, name: str):
    # The form's element ids match the API's field names, except the location search box.
    element_id = "candidate-location" if name == "location" else name
    return page.locator(f"[id='{element_id}']")


def choose_option(page: Page, name: str, option: str):
    """Pick an option in one of Greenhouse's custom dropdowns: open it, then click the option."""
    box = field_locator(page, name)
    box.click()
    choice = page.get_by_role("option", name=option.strip(), exact=True)
    if not choice.count():
        # Long lists only render matches after typing, and search boxes (e.g. School) only
        # search on real keystrokes.
        box.fill("")
        box.press_sequentially(option.strip()[:40], delay=30)
    choice.first.click(timeout=10000)


def fill_location(page: Page, city: str):
    """The location box searches as you type; picking a suggestion also sets the hidden coordinates."""
    box = field_locator(page, "location")
    box.click()
    box.press_sequentially(city, delay=50)  # the search only runs on real keystrokes
    page.get_by_role("option").first.click(timeout=10000)


EDUCATION_FIELDS = [  # (element id, profile["education"] key, label shown to the user)
    ("school--0", "school", "School"),
    ("degree--0", "degree", "Degree"),
    ("discipline--0", "discipline", "Discipline"),
    ("start-month--0", "start_month", "Education start month"),
    ("start-year--0", "start_year", "Education start year"),
    ("end-month--0", "end_month", "Education end month"),
    ("end-year--0", "end_year", "Education end year"),
]


def fill_education(page: Page, education: dict) -> list[str]:
    """Fill the education section, which isn't in the questions API. Returns labels left blank.

    A value can be a list of spellings, since companies name the same option differently
    (e.g. "University of California - Santa Cruz" vs "University of California, Santa Cruz").
    """
    blank = []
    for element_id, key, label in EDUCATION_FIELDS:
        box = field_locator(page, element_id)
        if not box.count():
            continue
        value = education.get(key, "")
        spellings = [v.strip() for v in (value if isinstance(value, list) else [value]) if v.strip()]
        for spelling in spellings:
            try:
                if box.get_attribute("role") == "combobox":
                    choose_option(page, element_id, spelling)
                else:
                    box.fill(spelling)
                break
            except PlaywrightError:
                continue
        else:
            blank.append(label)
    return blank


def fill_form(page: Page, app: dict, profile: dict) -> list[str]:
    """Fill every prepared answer. Returns labels of fields that couldn't be filled."""
    resume_path = os.path.abspath(profile["resume_path"])
    failed = []

    # The phone's country dropdown isn't in the questions API either.
    if field_locator(page, "country").count():
        try:
            choose_option(page, "country", profile["basics"].get("phone_country", ""))
        except PlaywrightError:
            failed.append("Phone country")

    for answer in app["answers"]:
        for name, value in answer["values"].items():
            try:
                box = field_locator(page, name)
                if not box.count():
                    raise PlaywrightError("field not found on page")
                if name == "resume":
                    box.set_input_files(resume_path)
                elif name == "location":
                    fill_location(page, value)
                elif box.get_attribute("role") == "combobox":
                    choose_option(page, name, value)
                else:
                    box.fill(value)
            except PlaywrightError:
                failed.append(answer["label"])

    return failed + fill_education(page, profile.get("education", {}))


def fill_all(profile: dict, company_filter: str):
    applications = load_json(APPLICATIONS_FILE, {})
    submitted = load_json(SUBMITTED_FILE, {})
    todo = [
        (job_id, app)
        for job_id, app in applications.items()
        if app["status"] in ("ready", "needs_input") and company_filter.lower() in app["company"].lower()
    ]
    if not todo:
        raise SystemExit("No prepared applications to fill. Run `python apply.py` first.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        for i, (job_id, app) in enumerate(todo, 1):
            print(f"\n[{i}/{len(todo)}] {app['company']} | {app['title']} | {app['location']}")
            if not browser.is_connected():  # the user closed the whole window
                browser = p.chromium.launch(headless=False)
            page = browser.new_page()
            try:
                # Wait for the form itself: some pages keep loading in the background and never go idle.
                page.goto(app["url"], wait_until="domcontentloaded")
                field_locator(page, "first_name").wait_for(timeout=20000)
                try:
                    # Anything typed before the page's scripts finish loading gets wiped.
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightError:
                    pass  # some pages never go fully idle; fill anyway
                failed = fill_form(page, app, profile)

                blanks = [label.strip() for label in app["unanswered"]] + failed
                if blanks:
                    print("  Fill these in yourself:")
                    for label in blanks:
                        print(f"    - {label[:100]}")
                else:
                    print("  Everything is filled in.")
            except PlaywrightError:
                # e.g. the company redirects to its own careers site, or a bot check blocks the form.
                print(f"  Couldn't find the application form (page is at {page.url}).")
                print("  Apply by hand in the window if you want, or skip.")

            choice = ""
            while choice not in ("y", "s", "q"):
                choice = input("  Submit in the browser, then type y = submitted, s = skip, q = quit: ").strip().lower()
            if choice == "q":
                return
            if choice == "y":
                del applications[job_id]
                app["status"] = "submitted"
                app["submitted_at"] = datetime.now(timezone.utc).isoformat()
                submitted[job_id] = app
                save_json(SUBMITTED_FILE, submitted)
            else:
                app["status"] = "skipped"
            save_json(APPLICATIONS_FILE, applications)  # save after every job, so quitting never loses progress
            try:
                page.close()
            except PlaywrightError:
                pass  # already closed by the user
        if browser.is_connected():
            browser.close()


def main():
    profile = load_json(PROFILE_FILE, None)
    if profile is None:
        raise SystemExit(f"{PROFILE_FILE} not found")
    if not os.path.exists(profile["resume_path"]):
        print(f"Warning: resume not found at {profile['resume_path']}")

    if len(sys.argv) > 1 and sys.argv[1] == "fill":
        fill_all(profile, company_filter=" ".join(sys.argv[2:]))
    else:
        prepare_all(profile)


if __name__ == "__main__":
    main()
