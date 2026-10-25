import time

import requests

MAX_RETRIES = 3


def get(url: str, timeout: int = 15) -> requests.Response:
    """GET that waits and retries when the server rate-limits us (HTTP 429)."""
    for _ in range(MAX_RETRIES):
        response = requests.get(url, timeout=timeout)
        if response.status_code != 429:
            break
        time.sleep(int(response.headers.get("Retry-After", 5)))
    response.raise_for_status()
    return response
