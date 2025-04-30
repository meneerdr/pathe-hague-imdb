#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape.py  –  Pathé Den-Haag titles  ➜  pretty mobile-friendly HTML

◆ Requires : requests
◆ Optional : concurrent.futures (in stdlib)
◆ Needs     : template.html (in repo root)

Environment
-----------
OMDB_KEY or OMDB_API_KEY   –  OMDb / IMDb API key
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import logging
import os
import sys
from typing import Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3 import Retry

# ───────────────────────── CONFIG ──────────────────────────
PATHÉ_SHOWS_URL = "https://www.pathe.nl/api/shows"
PAGE_SIZES = (100, 50, 20)           # try these until one works
DEFAULT_TIMEOUT = 30                 # seconds

RETRIES = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=2,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods={"GET"},
)

HEADERS: Dict[str, str] = {          # identical to the one cURL that works
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "sec-ch-ua":    '"Google Chrome";v="135", "Not-A.Brand";v="8", "Chromium";v="135"',
    "sec-ch-ua-platform": '"macOS"',
    "sec-ch-ua-mobile":   "?0",
    "Referer":      "https://www.pathe.nl/nl/bioscopen/pathe-buitenhof",
    "DNT":          "1",
    "Connection":   "close",
}

# OMDb / IMDb ----------------------------------------------------------
OMDB_URL     = "https://www.omdbapi.com/"
OMDB_KEY_ENV = os.getenv("OMDB_API_KEY") or os.getenv("OMDB_KEY")
MAX_OMDB_WORKERS = 10  # concurrent threads

# ───────────────────────── logging ─────────────────────────
LOG = logging.getLogger("pathe")
LOG.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(levelname)-8s %(asctime)s  %(message)s", "%H:%M:%S"))
LOG.addHandler(_handler)

# ──────────────────────── HTTP session ─────────────────────
def build_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(HEADERS)
    sess.mount("https://", HTTPAdapter(max_retries=RETRIES, pool_connections=4, pool_maxsize=4))
    return sess

SESSION = build_session()

def get_json(url: str, *, params: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    LOG.debug("GET %s  params=%s", url, params)
    resp = SESSION.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

# ─────────────────────── Pathé scraping ────────────────────
def fetch_shows(date: str) -> List[dict]:
    for size in PAGE_SIZES:
        try:
            data = get_json(
                PATHÉ_SHOWS_URL,
                params={"language": "nl", "date": date, "pageSize": size},
            )
            shows = data.get("shows", [])
            LOG.info("· got %d shows (pageSize=%d)", len(shows), size)
            if shows:
                return shows
        except Exception as exc:
            LOG.warning("pageSize=%d failed – %s", size, exc)
    LOG.critical("💥 Could not retrieve any shows for %s", date)
    sys.exit(1)

# ─────────────────────── IMDb enrichment ───────────────────
def fetch_rating(title: str, key: str) -> Optional[str]:
    try:
        data = get_json(
            OMDB_URL,
            params={"apikey": key, "t": title, "r": "json"},
            timeout=10,
        )
        if data.get("Response") == "True":
            rating = data.get("imdbRating")
            if rating and rating != "N/A":
                return rating
    except Exception as exc:  # noqa: BLE001
        LOG.debug("OMDb lookup failed for %s – %s", title, exc)
    return None

def add_imdb_ratings(shows: List[dict], key: str) -> None:
    LOG.info("🔍 fetching IMDb ratings … (max %d threads)", MAX_OMDB_WORKERS)
    with cf.ThreadPoolExecutor(max_workers=MAX_OMDB_WORKERS) as pool:
        futures = {pool.submit(fetch_rating, s["title"], key): s for s in shows}
        for fut in cf.as_completed(futures):
            shows_item = futures[fut]
            shows_item["imdbRating"] = fut.result() or "—"

# ───────────────────────── HTML output ─────────────────────
def build_html(shows: List[dict], date: str) -> str:
    """Render template.html → full HTML page with mobile-friendly cards."""
    tpl = open("template.html", encoding="utf-8").read()

    cards: list[str] = []
    for s in shows:
        title       = s["title"]
        rel         = ", ".join(s.get("releaseAt", []))
        imdb        = s.get("imdbRating", "—")
        poster_md   = s.get("posterPath", {}).get("md", "")
        safe_title  = title.replace("&", "&amp;")
        cards.append(f"""
      <div class="card">
        <img src="{poster_md}" alt="Poster: {safe_title}">
        <div class="info">
          <h2 class="title">{safe_title}</h2>
          <div class="meta">Release: {rel}<br>IMDb: {imdb}</div>
        </div>
      </div>
        """.strip())

    page = tpl.replace("{{cards}}", "\n".join(cards))
    page = page.replace("{{date}}", date)
    page = page.replace("{{year}}", str(dt.date.today().year))
    return page

# ────────────────────────── CLI / main ─────────────────────
def parse_args() -> argparse.Namespace:
    today = dt.date.today().isoformat()
    p = argparse.ArgumentParser(description="Fetch Pathé Den-Haag shows & build mobile HTML")
    p.add_argument("--date",    default=today, metavar="YYYY-MM-DD",
                   help="date to query (default: today)")
    p.add_argument("--imdb-key", help="override OMDb key")
    p.add_argument("--skip-imdb", action="store_true", help="disable IMDb enrichment")
    p.add_argument("--debug",    action="store_true", help="verbose logging")
    p.add_argument("--output",   default="public/index.html",
                   help="output HTML file (default: public/index.html)")
    return p.parse_args()

def main() -> None:
    args = parse_args()
    if args.debug:
        LOG.setLevel(logging.DEBUG)

    imdb_key = args.imdb_key or OMDB_KEY_ENV
    if imdb_key:
        LOG.info("✅ IMDb key loaded from %s", "CLI" if args.imdb_key else "environment")
    else:
        LOG.info("ℹ️  No IMDb key found – skipping ratings")

    LOG.info("🔗  Querying Pathé JSON API for %s …", args.date)
    shows = fetch_shows(args.date)

    if imdb_key and not args.skip_imdb:
        add_imdb_ratings(shows, imdb_key)
    else:
        for s in shows:
            s["imdbRating"] = "—"

    html = build_html(shows, args.date)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(html)
    LOG.info("✅ wrote %s (%d bytes)", args.output, len(html))

if __name__ == "__main__":
    main()

