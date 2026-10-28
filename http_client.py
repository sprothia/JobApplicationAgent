import time

import requests

MAX_RETRIES = 5


def request(method: str, url: str, timeout: int = 15, **kwargs) -> requests.Response:
    """HTTP request that waits and retries when the server rate-limits us (HTTP 429).

    Waits as long as the server asks (Retry-After), or else 5s, 10s, 20s, 40s.
    """
    for attempt in range(MAX_RETRIES):
        response = requests.request(method, url, timeout=timeout, **kwargs)
        if response.status_code != 429 or attempt == MAX_RETRIES - 1:
            break
        time.sleep(int(response.headers.get("Retry-After", 5 * 2**attempt)))
    response.raise_for_status()
    return response


def get(url: str, timeout: int = 15) -> requests.Response:
    return request("GET", url, timeout=timeout)


def post(url: str, json: dict, timeout: int = 15) -> requests.Response:
    return request("POST", url, timeout=timeout, json=json)
