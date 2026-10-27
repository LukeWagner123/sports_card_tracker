"""Shared HTTP fetcher for every source.

Enforces the rules that keep collection reliable and polite:
- robots.txt is checked before any fetch; disallowed URLs are never requested.
- a minimum interval between requests to the same host.
- retries with exponential backoff on 429 / 5xx / network errors, honoring Retry-After.
- a 403 or bot challenge raises BlockedError immediately; it is never retried or worked around.
"""

import time
import urllib.robotparser
from urllib.parse import urlsplit

import requests

USER_AGENT = "cardtracker/0.1 (sports card research)"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class BlockedError(RuntimeError):
    """The site refused us (403 / bot challenge). Stop; do not retry or evade."""


class DisallowedError(RuntimeError):
    """robots.txt disallows this URL."""


class Fetcher:
    def __init__(self, min_interval: float = 3.0, max_retries: int = 4, backoff: float = 2.0,
                 timeout: float = 30.0, session: requests.Session | None = None,
                 check_robots: bool = True, sleep=time.sleep):
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff = backoff
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.check_robots = check_robots
        self._sleep = sleep
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                resp = self.session.get(f"{origin}/robots.txt", timeout=self.timeout)
                rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            self._robots[origin] = rp
        return self._robots[origin].can_fetch(self.session.headers["User-Agent"], url)

    def _throttle(self, host: str) -> None:
        wait = self._last_request.get(host, 0.0) + self.min_interval - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last_request[host] = time.monotonic()

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        if self.check_robots and not self._allowed(url):
            raise DisallowedError(url)
        host = urlsplit(url).netloc
        kwargs.setdefault("timeout", self.timeout)
        for attempt in range(self.max_retries + 1):
            self._throttle(host)
            try:
                resp = self.session.request(method, url, **kwargs)
            except requests.RequestException:
                if attempt == self.max_retries:
                    raise
                self._sleep(self.backoff * 2 ** attempt)
                continue
            if resp.status_code == 403 or "cf-mitigated" in resp.headers:
                raise BlockedError(f"{resp.status_code} from {url}")
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else self.backoff * 2 ** attempt
                self._sleep(delay)
                continue
            resp.raise_for_status()
            return resp
        raise AssertionError("unreachable")
