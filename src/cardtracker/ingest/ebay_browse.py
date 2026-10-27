"""eBay Browse API client — active listings only (asking prices, current bids, supply).

Docs: https://developer.ebay.com/api-docs/buy/browse/overview.html
Auth is OAuth client-credentials with the public api_scope. Default quota is 5,000 calls/day.
"""

import base64
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from cardtracker.fetcher import Fetcher

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
API_BASE = "https://api.ebay.com/buy/browse/v1"
SCOPE = "https://api.ebay.com/oauth/api_scope"
SPORTS_TRADING_CARDS_CATEGORY = "261328"

GRADE_RE = re.compile(r"\b(PSA|BGS|SGC|CGC)\s*(10|[1-9](?:\.5)?)\b", re.IGNORECASE)

# Title terms that mean the listing is not a genuine single copy of the card.
GLOBAL_EXCLUDE = ["reprint", "rp", "lot", "custom", "digital", "facsimile", "proxy", "you pick", "u pick"]


class EbayBrowseClient:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 fetcher: Fetcher | None = None):
        self.client_id = client_id or os.environ.get("EBAY_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("EBAY_CLIENT_SECRET")
        if not (self.client_id and self.client_secret):
            raise RuntimeError("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not set")
        # API endpoints, not crawled pages: robots.txt does not apply.
        self.fetcher = fetcher or Fetcher(min_interval=0.5, check_robots=False)
        self._token: str | None = None
        self._token_expires = 0.0

    def _auth(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        resp = self.fetcher.session.post(
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

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._auth()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}

    def search_auctions(self, query: str, limit: int = 200) -> dict:
        """Active auctions for a query in Sports Trading Cards, soonest-ending first."""
        resp = self.fetcher.get(
            f"{API_BASE}/item_summary/search",
            headers=self._headers(),
            params={"q": query, "category_ids": SPORTS_TRADING_CARDS_CATEGORY,
                    "filter": "buyingOptions:{AUCTION}", "sort": "endingSoonest", "limit": limit},
        )
        return resp.json()

    def get_item(self, item_id: str) -> dict:
        resp = self.fetcher.get(f"{API_BASE}/item/{quote(item_id, safe='')}", headers=self._headers())
        return resp.json()


def parse_grade(title: str) -> tuple[str, float | None]:
    """Extract (grader, grade) from a listing title; ('RAW', None) if none found."""
    m = GRADE_RE.search(title)
    if not m:
        return "RAW", None
    return m.group(1).upper(), float(m.group(2))


def title_matches(title: str, required: list[str], exclude: list[str]) -> bool:
    """True if every required term appears and no excluded term does (whole words, case-insensitive)."""
    t = title.lower()

    def has(term: str) -> bool:
        return re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", t) is not None

    return all(has(r) for r in required) and not any(has(e) for e in GLOBAL_EXCLUDE + exclude)


def parse_ebay_time(value: str | None) -> str | None:
    """'2026-10-28T03:12:45.000Z' -> '2026-10-28T03:12:45+00:00' (same format as db.now_iso)."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    return dt.isoformat(timespec="seconds")


def auction_observation(item: dict) -> dict | None:
    """Normalize a search summary or getItem payload; None if not a USD auction."""
    if "AUCTION" not in item.get("buyingOptions", []):
        return None
    price = item.get("currentBidPrice") or item.get("price") or {}
    if price.get("currency") != "USD" or "value" not in price:
        return None
    return {
        "item_id": item["itemId"],
        "title": item.get("title", ""),
        "price": float(price["value"]),
        "bid_count": int(item.get("bidCount", 0)),
        "ends_at": parse_ebay_time(item.get("itemEndDate")),
    }
