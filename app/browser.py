"""Fill application forms in a visible browser. The user always clicks Submit."""

import re
import textwrap
from datetime import datetime

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from answers import pick_option, remember_answer, resume_file
from config.constants import APPLICATIONS_FILE
from forms import BROWSER_ONLY_TYPES, IGNORED_FIELDS
from utils.storage import load_json, record_jobs

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
    resume_path = resume_file(profile)
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
    resume_path = resume_file(profile)
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


def short_location(location: str) -> str:
    """'A; B; C; D' -> 'A · B +2 more', so long location lists stay short."""
    location = re.sub(r",\s*(United States|USA|US)\b", "", location)
    parts = [p.strip() for p in re.split(r"[;|]", location) if p.strip()]
    if len(parts) <= 2:
        return " · ".join(parts)
    return f"{parts[0]} · {parts[1]} +{len(parts) - 2} more"


def ask_user(page: Page, app: dict, profile: dict, is_ashby: bool, ask, tell) -> list[str]:
    """Ask each unanswered required question and fill the answer in on the page.
    ask shows a question and returns the reply; tell shows a message.
    Returns the questions left for the user to do in the browser."""
    questions = {q["label"]: q for q in app.get("questions", [])}
    resume_path = resume_file(profile)
    left = []
    asked = 0
    for label in app["unanswered"]:
        fields = [
            f for f in questions.get(label, {}).get("fields", [])
            if f["name"] not in IGNORED_FIELDS and f["type"] != "input_hidden"
        ]
        if not fields or fields[0]["type"] in BROWSER_ONLY_TYPES:
            left.append(label)
            continue
        field = fields[0]
        options = field["options"] or (["Yes", "No"] if field["type"] == "Boolean" else [])

        asked += 1
        question = f"Q{asked}. " + textwrap.shorten(" ".join(label.split()), 300, placeholder="...") + "\n"
        question += "".join(f"  {i}. {option}\n" for i, option in enumerate(options, 1))
        question += "Reply with " + ("a number" if options else "your answer") + " (blank = do it in the browser)"
        reply = ask(question).strip()
        if not reply:
            left.append(label)
            continue
        if options:
            value = options[int(reply) - 1] if reply.isdigit() and 0 < int(reply) <= len(options) else pick_option([reply], options)
            if not value:
                tell("Not one of the options, so leaving it for the browser.")
                left.append(label)
                continue
        else:
            value = reply

        try:
            if is_ashby:
                fill_ashby_field(page, ashby_container(page, field["name"], label), field["type"], field["name"], value, resume_path)
            elif field_locator(page, field["name"]).get_attribute("role") == "combobox":
                choose_option(page, field["name"], value)
            else:
                field_locator(page, field["name"]).fill(value)
        except PlaywrightError:
            tell("Couldn't fill that in on the page, so leaving it for the browser.")
            left.append(label)
            continue
        if ask("Remember this for future forms? (y/n)").strip().lower() == "y":
            remember_answer(profile, label, value)
    return left


def fill_jobs(profile: dict, job_ids: list[str], ask, tell) -> list[dict]:
    """Open each job's form in a browser, fill it in, and ask about anything left blank.
    The user clicks Submit, then answers y (submitted), s (skip) or q (stop).
    ask shows a question and returns the reply; tell shows a message (the agent does both in chat).
    Returns what happened to each job: "submitted", "skipped", or "not done" (stopped before it)."""
    applications = load_json(APPLICATIONS_FILE, {})
    todo = [(job_id, applications[job_id]) for job_id in job_ids]
    results = [{"company": app["company"], "title": app["title"].strip(), "result": "not done"} for _, app in todo]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        for i, (job_id, app) in enumerate(todo, 1):
            tell(f"[{i}/{len(todo)}] Opening {app['company']} · {app['title'].strip()} ({short_location(app['location'])})")
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

                if app["unanswered"]:
                    tell(f"{len(app['unanswered'])} question(s) need your answer:")
                left = ask_user(page, app, profile, is_ashby, ask, tell) + failed
                if left:
                    tell("Do these in the browser:\n" + "\n".join(
                        "  - " + textwrap.shorten(" ".join(label.split()), 100, placeholder="...") for label in left
                    ))
                else:
                    tell("Everything is filled in.")
            except PlaywrightError:
                # e.g. the company redirects to its own careers site, or a bot check blocks the form.
                tell(f"Couldn't find the application form (page is at {page.url}). Apply by hand in the window if you want, or skip.")

            choice = ""
            while choice not in ("y", "s", "q"):
                choice = ask("Submit in the browser, then type y = submitted, s = skip, q = stop: ").strip().lower()
            if choice == "q":
                return results
            results[i - 1]["result"] = "submitted" if choice == "y" else "skipped"
            record_jobs([job_id], results[i - 1]["result"])  # saved after every job, so stopping never loses progress
            try:
                page.close()
            except PlaywrightError:
                pass  # already closed by the user
        if browser.is_connected():
            browser.close()
    return results
