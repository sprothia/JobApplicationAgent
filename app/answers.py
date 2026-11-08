"""Answer form questions from profile.json."""

import re

from config.constants import PROFILE_DIR, PROFILE_FILE
from forms import IGNORED_FIELDS, application_url
from get_jobs import Job
from utils.storage import save_json

# Greenhouse's standard fields, filled from profile["basics"] (resume comes from profile["resume_path"]).
BASIC_FIELDS = {"first_name", "last_name", "preferred_name", "email", "phone", "location"}


# Ashby field types filled from profile["basics"].
ASHBY_BASIC_TYPES = {"Email": "email", "Phone": "phone", "Location": "location"}


# Greenhouse EEO fields: several sit under one label (e.g. "Race" holds hispanic_ethnicity and race).
EEO_FIELDS = {"gender", "race", "hispanic_ethnicity", "veteran_status", "disability_status"}


def resume_file(profile: dict) -> str:
    """The resume's full path. profile.json names it relative to the profile folder."""
    return str(PROFILE_DIR / profile["resume_path"])


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
        return resume_file(profile)
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
        "posted_date": job.posted_date,
        "source": job.source,
        "board_token": job.board_token,
        "url": application_url(job),
        "status": "needs_input" if unanswered else "ready",
        "unanswered": unanswered,
        "answers": answers,
        "questions": questions,  # saved so later runs don't download the form again
    }


def remember_answer(profile: dict, label: str, value: str):
    """Save an answer under the question's exact wording, so future forms asking it get it automatically."""
    keyword = " ".join(label.split()).lower().rstrip("*").strip()
    profile["answers"].insert(0, {"keywords": [keyword], "answer": value})  # first, so it wins
    save_json(PROFILE_FILE, profile)
