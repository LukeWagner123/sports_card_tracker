"""PSA Public API client.

Docs: https://www.psacard.com/publicapi — requires a free bearer token and has a low
daily call limit, so callers should only fetch cards on the watchlist.
Endpoint paths below follow the published docs; verify against them when a token is available.
"""

import os
import sqlite3
from datetime import date

import requests

from cardtracker.db import store_raw

BASE_URL = "https://api.psacard.com/publicapi"


class PSAClient:
    def __init__(self, token: str | None = None, session: requests.Session | None = None):
        self.token = token or os.environ.get("PSA_API_TOKEN")
        if not self.token:
            raise RuntimeError("PSA_API_TOKEN is not set")
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"bearer {self.token}"

    def _get(self, path: str) -> dict:
        resp = self.session.get(f"{BASE_URL}{path}", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_cert(self, cert_number: str) -> dict:
        return self._get(f"/cert/GetByCertNumber/{cert_number}")

    def get_spec_population(self, spec_id: int) -> dict:
        return self._get(f"/pop/GetPSASpecPopulation/{spec_id}")


def parse_population(payload: dict) -> dict[float, int]:
    """Map a spec-population payload to {grade: count}.

    PSA returns grade counts as fields like 'Grade10', 'Grade9', 'Grade8_5'.
    """
    pop = payload.get("PSAPop") or payload
    counts: dict[float, int] = {}
    for key, value in pop.items():
        if not key.startswith("Grade") or not isinstance(value, int):
            continue
        grade_str = key.removeprefix("Grade").replace("_", ".")
        try:
            counts[float(grade_str)] = value
        except ValueError:
            continue
    return counts


def snapshot_population(conn: sqlite3.Connection, client: PSAClient, card_id: int, spec_id: int) -> int:
    """Fetch PSA population for one card and store a dated snapshot. Returns rows written."""
    payload = client.get_spec_population(spec_id)
    store_raw(conn, "psa", str(spec_id), payload)
    today = date.today().isoformat()
    counts = parse_population(payload)
    conn.executemany(
        "INSERT OR IGNORE INTO pop_snapshots (card_id, grader, grade, population, snapshot_date) "
        "VALUES (?, 'PSA', ?, ?, ?)",
        [(card_id, grade, count, today) for grade, count in counts.items()],
    )
    conn.commit()
    return len(counts)
