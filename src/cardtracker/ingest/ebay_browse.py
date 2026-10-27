"""eBay Browse API client — active listings only (asking prices and supply).

Docs: https://developer.ebay.com/api-docs/buy/browse/overview.html
Auth is OAuth client-credentials with the public api_scope.
"""

import base64
import os
import re
import sqlite3
import time

import requests

from cardtracker.db import now_iso, store_raw

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
SPORTS_TRADING_CARDS_CATEGORY = "261328"

GRADE_RE = re.compile(r"\b(PSA|BGS|SGC|CGC)\s*(10|[1-9](?:\.5)?)\b", re.IGNORECASE)


class EbayBrowseClient:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 session: requests.Session | None = None):
        self.client_id = client_id or os.environ.get("EBAY_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("EBAY_CLIENT_SECRET")
        if not (self.client_id and self.client_secret):
            raise RuntimeError("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not set")
        self.session = session or requests.Session()
        self._token: str | None = None
        self._token_expires = 0.0

    def _auth(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        resp = self.session.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": SCOPE},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + body["expires_in"]
        return self._token

    def search(self, query: str, limit: int = 200, offset: int = 0) -> dict:
        resp = self.session.get(
            SEARCH_URL,
            headers={"Authorization": f"Bearer {self._auth()}",
                     "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
            params={"q": query, "category_ids": SPORTS_TRADING_CARDS_CATEGORY,
                    "limit": limit, "offset": offset},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


def parse_grade(title: str) -> tuple[str, float | None]:
    """Extract (grader, grade) from a listing title; ('RAW', None) if none found."""
    m = GRADE_RE.search(title)
    if not m:
        return "RAW", None
    return m.group(1).upper(), float(m.group(2))


def collect_listings(conn: sqlite3.Connection, client: EbayBrowseClient, card_id: int, query: str) -> int:
    """Snapshot current active listings for one watchlist card. Returns rows written."""
    payload = client.search(query)
    store_raw(conn, "ebay_browse", query, payload)
    observed = now_iso()
    rows = []
    for item in payload.get("itemSummaries", []):
        price = item.get("price", {})
        if price.get("currency") != "USD":
            continue
        grader, grade = parse_grade(item.get("title", ""))
        rows.append((card_id, grader, grade, float(price["value"]),
                     ",".join(item.get("buyingOptions", [])), observed,
                     item.get("itemEndDate"), "ebay_browse", item["itemId"], item.get("title")))
    conn.executemany(
        "INSERT OR IGNORE INTO listings (card_id, grader, grade, price_usd, listing_type, observed_at, "
        "ends_at, source, external_id, raw_title) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)
