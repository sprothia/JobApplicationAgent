"""Prepare and fill Greenhouse applications from profile.json.

  python apply.py                 Prepare: for each Greenhouse job from get_jobs(), fetch its form and
                                  answer every question from the profile. Questions the profile can't
                                  answer are left blank: if any are required, the job is marked
                                  'needs_input' and they're listed. Results go to applications.json.
  python apply.py fill [filter]   Fill: open each prepared job in a browser and fill it in. You review,
                                  answer anything left blank and click Submit yourself. Jobs you mark
                                  submitted move from applications.json to submitted.json.
                                  The optional filter is a company ("Notion") or a platform
                                  ("ashby" / "greenhouse").
"""

import json
import os
import re
import sys
import time
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

# Ashby has no public questions API. This is the request Ashby's own application page makes to
# load its form, trimmed to the form fields.
ASHBY_FORM_URL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
ASHBY_FORM_QUERY = """
query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
    applicationForm { sections { fieldEntries { field isRequired isHidden } } }
    surveyForms { sections { fieldEntries { field isRequired isHidden } } }
  }
}
"""

# Greenhouse's standard fields, filled from profile["basics"] (resume comes from profile["resume_path"]).
BASIC_FIELDS = {"first_name", "last_name", "preferred_name", "email", "phone", "location"}
# Ashby field types filled from profile["basics"].
ASHBY_BASIC_TYPES = {"Email": "email", "Phone": "phone", "Location": "location"}
# Greenhouse EEO fields: several sit under one label (e.g. "Race" holds hispanic_ethnicity and race).
EEO_FIELDS = {"gender", "race", "hispanic_ethnicity", "veteran_status", "disability_status"}
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


def fetch_ashby_questions(job: Job) -> list[dict]:
    """All questions on an Ashby form, in the same shape as fetch_questions() returns for Greenhouse.

    Field types are Ashby's own: String, Email, Phone, File, Location, Boolean, Date, LongText,
    ValueSelect (one choice) and MultiValueSelect (several choices).
    """
    variables = {"organizationHostedJobsPageName": job.board_token, "jobPostingId": job.id}
    data = http_client.post(ASHBY_FORM_URL, json={"query": ASHBY_FORM_QUERY, "variables": variables}).json()
    posting = (data.get("data") or {}).get("jobPosting")
    if not posting:  # the posting was taken down
        return []

    # The application form, plus any voluntary surveys (e.g. EEO) shown with it.
    forms = [posting.get("applicationForm")] + list(posting.get("surveyForms") or [])
    questions = []
    for form in filter(None, forms):
        for section in form["sections"]:
            for entry in section["fieldEntries"]:
                field = entry["field"]
                if entry.get("isHidden") or field.get("isHidden"):
                    continue
                questions.append(
                    {
                        "label": field["title"],
                        "required": entry["isRequired"],
                        "fields": [
                            {
                                "name": field["path"],
                                "type": field["type"],
                                "options": [v["label"] for v in field.get("selectableValues") or []],
                            }
                        ],
                    }
                )
    return questions


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
    """Answer one form field (Greenhouse or Ashby) from the profile; '' if it can't be answered."""
    name, field_type = field["name"], field["type"]
    basics = profile["basics"]
    if name in ("resume", "_systemfield_resume"):
        return profile["resume_path"]
    if name == "_systemfield_name":
        return f"{basics['first_name']} {basics['last_name']}"
    if name in BASIC_FIELDS:
        return basics.get(name, "").strip()
    if field_type in ASHBY_BASIC_TYPES:
        return basics.get(ASHBY_BASIC_TYPES[field_type], "").strip()
    if field_type in ("input_file", "File", "EducationHistory"):  # other uploads, Ashby's education block
        return ""

    # Include the field name so each EEO field matches its own profile entry.
    if name in EEO_FIELDS:
        label = f"{name.replace('_', ' ')} {label}"
    phrasings = find_answer(label, profile)
    if not phrasings:
        return ""
    if field_type.startswith("multi_value") or field_type in ("ValueSelect", "MultiValueSelect"):
        return pick_option(phrasings, field["options"])
    if field_type == "Boolean":  # Ashby's Yes/No buttons
        return pick_option(phrasings, ["Yes", "No"])
    return phrasings[0]  # text fields use the first phrasing


def application_url(job: Job) -> str:
    if job.source == "ashby":
        # ?embed=js is the version companies embed on their own sites; it still works when a company
        # has turned off Ashby's hosted page (e.g. Cursor, where the plain link shows "Page not found").
        return f"https://jobs.ashbyhq.com/{job.board_token}/{job.id}/application?embed=js"
    # The embed URL shows Greenhouse's form directly; the normal job URL redirects to the
    # company's own careers site for some companies (Pinterest, Waymo, Nuro, Roblox, ...).
    return f"https://job-boards.greenhouse.io/embed/job_app?for={job.board_token}&token={job.id}"


def prepare(job: Job, profile: dict, questions: list[dict]) -> dict:
    """Answer every question on the job's form. Status is 'ready', or 'needs_input' if a required one is blank."""
    answers, unanswered = [], []
    for question in questions:
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
        "source": job.source,
        "url": application_url(job),
        "status": "needs_input" if unanswered else "ready",
        "unanswered": unanswered,
        "answers": answers,
        "questions": questions,  # saved so later runs don't download the form again
    }


def prepare_all(profile: dict):
    applications = load_json(APPLICATIONS_FILE, {})
    submitted = load_json(SUBMITTED_FILE, {})
    jobs = [
        job
        for job in get_jobs()
        if job.source in ("greenhouse", "ashby")
        and job.id not in submitted
        and applications.get(job.id, {}).get("status") != "skipped"  # skips are permanent
    ]
    print(f"Preparing {len(jobs)} jobs ({Counter(job.source for job in jobs)})...")

    for job in jobs:
        # Forms don't change, so download each one once and reuse it. Answers are still
        # recomputed every run, so profile changes apply.
        questions = applications.get(job.id, {}).get("questions")
        if not questions:
            try:
                questions = fetch_ashby_questions(job) if job.source == "ashby" else fetch_questions(job)
            except requests.RequestException as e:
                print(f"Warning: failed to fetch form for {job.company} - {job.title}: {e}")
                continue
            if job.source == "ashby":
                time.sleep(1)  # Ashby rate-limits its form requests
        if questions:  # no questions means the posting was taken down
            applications[job.id] = prepare(job, profile, questions)

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


# ---------- Fill (Ashby) ----------

def ashby_container(page: Page, path: str, label: str):
    """The block holding one question: found by the question's id on its input, or else by its label
    (location and date boxes carry no id)."""
    by_id = page.locator(
        f"[class*='fieldEntry']:has([id='{path}'], [name='{path}'], [name$='_{path}'], [id*='_{path}-labeled'])"
    )
    if by_id.count():
        return by_id.first
    # Exact label (plus an optional "*" for required), so "Location" doesn't match "Relocation preference".
    exact_label = page.locator("label").filter(has_text=re.compile(rf"^\s*{re.escape(label.strip())}\s*\*?\s*$"))
    return page.locator("[class*='fieldEntry']").filter(has=exact_label).first


def ashby_date(value: str) -> str:
    """Ashby's date box takes MM/DD/YYYY. Accepts e.g. 'March 2027' (-> 03/01/2027)."""
    for fmt in ("%B %Y", "%b %Y", "%m/%d/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    raise PlaywrightError(f"can't read date {value!r}")


def fill_ashby_field(page: Page, box, field_type: str, path: str, value: str, resume_path: str):
    if field_type == "File":
        box.locator("input[type=file]").set_input_files(resume_path)
    elif field_type == "Boolean":  # Yes/No buttons
        box.get_by_role("button", name=value, exact=True).click()
    elif field_type == "Location":
        search = box.get_by_role("combobox")
        search.click()
        search.press_sequentially(value, delay=40)  # the search only runs on real keystrokes
        page.get_by_role("option").first.click(timeout=10000)
    elif field_type == "Date":
        date_box = box.locator("input").first
        date_box.click()
        date_box.press_sequentially(ashby_date(value), delay=30)
        date_box.press("Enter")
    elif field_type in ("ValueSelect", "MultiValueSelect"):
        dropdown = box.get_by_role("combobox")
        if dropdown.count():  # long option lists show as a dropdown
            dropdown.click()
            dropdown.press_sequentially(value[:40], delay=30)
            page.get_by_role("option", name=value, exact=True).first.click(timeout=10000)
        else:  # short lists show as radio buttons or checkboxes
            box.get_by_text(value, exact=True).first.click()
    else:  # String, Email, Phone, LongText
        box.locator(f"[id='{path}']").fill(value)


def fill_ashby_form(page: Page, app: dict, profile: dict) -> list[str]:
    """Fill every prepared answer on an Ashby form. Returns labels of fields that couldn't be filled."""
    resume_path = os.path.abspath(profile["resume_path"])
    types = {f["name"]: f["type"] for q in app["questions"] for f in q["fields"]}
    failed = []
    for answer in app["answers"]:
        for path, value in answer["values"].items():
            try:
                box = ashby_container(page, path, answer["label"])
                if not box.count():
                    raise PlaywrightError("field not found on page")
                fill_ashby_field(page, box, types.get(path, "String"), path, value, resume_path)
            except PlaywrightError:
                failed.append(answer["label"])
    return failed


def fill_all(profile: dict, job_filter: str):
    """job_filter is a company name, or a platform ("ashby" / "greenhouse"); empty means every job."""
    applications = load_json(APPLICATIONS_FILE, {})
    submitted = load_json(SUBMITTED_FILE, {})
    job_filter = job_filter.lower()
    todo = [
        (job_id, app)
        for job_id, app in applications.items()
        if app["status"] in ("ready", "needs_input")
        and (job_filter in app["company"].lower() or job_filter == app.get("source", "greenhouse"))
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
                is_ashby = app.get("source") == "ashby"
                # Wait for the form itself: some pages keep loading in the background and never go idle.
                page.goto(app["url"], wait_until="domcontentloaded")
                first_field = page.locator("[class*='fieldEntry']") if is_ashby else field_locator(page, "first_name")
                first_field.first.wait_for(timeout=20000)
                try:
                    # Anything typed before the page's scripts finish loading gets wiped.
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightError:
                    pass  # some pages never go fully idle; fill anyway
                failed = fill_ashby_form(page, app, profile) if is_ashby else fill_form(page, app, profile)

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
        fill_all(profile, job_filter=" ".join(sys.argv[2:]))
    else:
        prepare_all(profile)


if __name__ == "__main__":
    main()
