from pathlib import Path

# Where files live. Paths are absolute so scripts work from any folder.
ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"  # job status (gitignored)
PROFILE_DIR = ROOT / "profile"  # your info and resume (gitignored)
PROFILE_FILE = PROFILE_DIR / "profile.json"
APPLICATIONS_FILE = DATA_DIR / "applications.json"  # jobs still to apply to
SUBMITTED_FILE = DATA_DIR / "submitted.json"  # jobs you've applied to
YC_BOARDS_FILE = DATA_DIR / "yc_boards.json"  # YC startups' job boards

# Companies to track live in SETTINGS_FILE so the agent can change them by chat:
#   {"greenhouse": {name: board token}, "ashby": {name: board token}, "ignored_companies": [names]}
SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{token}"

# YC startups: the refresh_yc_startups tool finds their job boards and saves them to YC_BOARDS_FILE.
YC_URL = "https://yc-oss.github.io/api/companies/hiring.json"
YC_LOCATION = "San Francisco"
YC_MAX_TEAM_SIZE = 200

LEVEL_KEYWORDS = ["new grad", "new graduate", "early career", "university", "entry level", "entry-level"]
ROLE_KEYWORDS = [
    "software engineer",
    "software developer",
    "engineer, software",
    "forward deployed",
    "ai engineer",
    "machine learning engineer",
    "ml engineer",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "fullstack engineer",
    "full-stack engineer",
    "product engineer",
    "founding engineer",
    "infrastructure engineer",
    "member of technical staff",
]
# Jobs whose location mentions one of these (whole words, case-insensitive) are outside the US and dropped.
NON_US_LOCATION_KEYWORDS = [
    "canada", "toronto", "vancouver", "montreal",
    "united kingdom", "uk", "england", "london", "ireland", "dublin",
    "europe", "emea", "germany", "berlin", "munich", "france", "paris", "netherlands", "amsterdam",
    "spain", "barcelona", "madrid", "switzerland", "zurich", "sweden", "stockholm", "denmark", "aarhus",
    "poland", "warsaw", "romania", "bucharest", "serbia", "belgrade", "austria", "vienna",
    "israel", "tel aviv", "turkey", "türkiye", "istanbul", "kosovo",
    "india", "bangalore", "bengaluru", "hyderabad", "mumbai", "pune",
    "apac", "singapore", "japan", "tokyo", "korea", "seoul", "china", "hong kong", "taiwan",
    "australia", "sydney", "melbourne",
    "qatar", "doha", "uae", "dubai",
    "latam", "brazil", "são paulo", "mexico city", "argentina",
]

# Startup jobs with no level in the title are kept unless the title has one of these.
EXCLUDE_KEYWORDS = ["senior", "sr.", "sr ", "staff", "principal", "lead", "manager", "director", "head of", "intern"]
