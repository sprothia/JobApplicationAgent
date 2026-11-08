"""Download each job's application form: its questions, field types and dropdown options."""

from get_jobs import Job
from utils import http_client

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


# Alternatives to an upload (paste resume as text) and fields the browser fills itself (lat/long).
IGNORED_FIELDS = {"resume_text", "cover_letter_text", "latitude", "longitude"}


# Field types the user answers in the browser rather than the terminal (essays, uploads).
BROWSER_ONLY_TYPES = {"textarea", "LongText", "input_file", "File", "EducationHistory"}


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


def application_url(job: Job) -> str:
    if job.source == "ashby":
        # ?embed=js is the version companies embed on their own sites; it still works when a company
        # has turned off Ashby's hosted page (e.g. Cursor, where the plain link shows "Page not found").
        return f"https://jobs.ashbyhq.com/{job.board_token}/{job.id}/application?embed=js"
    # The embed URL shows Greenhouse's form directly; the normal job URL redirects to the
    # company's own careers site for some companies (Pinterest, Waymo, Nuro, Roblox, ...).
    return f"https://job-boards.greenhouse.io/embed/job_app?for={job.board_token}&token={job.id}"
