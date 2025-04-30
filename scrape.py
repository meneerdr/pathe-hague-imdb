#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape.py  –  Pathé Den Haag shows → pretty mobile-friendly HTML cards

Requires    : requests  pandas
Optional    : tabulate   (for local console preview only)

Environment :
  OMDB_API_KEY or OMDB_KEY   OMDb / IMDb API key
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import logging
import os
import sys
import time
from typing import Dict, List, Optional

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3 import Retry

# ───────────────────────── CONFIG ──────────────────────────
PATHE_SHOWS_URL = "https://www.pathe.nl/api/shows"
PAGE_SIZES      = (100, 50, 20)
DEFAULT_TIMEOUT = 30  # seconds
RETRIES = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=2,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods={"GET"},
)
HEADERS: Dict[str,str] = {
    # exactly matching the one cURL that always works
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "sec-ch-ua": '"Google Chrome";v="135", "Not-A.Brand";v="8", "Chromium";v="135"',
    "sec-ch-ua-platform": '"macOS"',
    "sec-ch-ua-mobile": "?0",
    "Referer": "https://www.pathe.nl/nl/bioscopen/pathe-buitenhof",
    "DNT": "1",
    "Connection": "close",
}

# OMDb / IMDb
OMDB_URL        = "https://www.omdbapi.com/"
OMDB_KEY_ENV    = os.getenv("OMDB_API_KEY") or os.getenv("OMDB_KEY")
MAX_OMDB_WORKERS= 10

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
            data = get_json(PATHE_SHOWS_URL, params={"language": "nl", "date": date, "pageSize": size})
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
        data = get_json(OMDB_URL, params={"apikey": key, "t": title, "r": "json"}, timeout=10)
        if data.get("Response") == "True":
            rating = data.get("imdbRating")
            if rating and rating != "N/A":
                return rating
    except Exception as exc:
        LOG.debug("OMDb lookup failed for %s – %s", title, exc)
    return None

def add_imdb_ratings(shows: List[dict], key: str) -> None:
    LOG.info("🔍 fetching IMDb ratings … (max %d threads)", MAX_OMDB_WORKERS)
    with cf.ThreadPoolExecutor(max_workers=MAX_OMDB_WORKERS) as pool:
        futures = {pool.submit(fetch_rating, s["title"], key): s for s in shows}
        for fut in cf.as_completed(futures):
            futures[fut]["imdbRating"] = fut.result() or "—"

# ────────────────────────── CSS + HTML ─────────────────────
CARD_CSS = """
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  margin: 0; padding: 1rem; background: #fafafa;
}
h1 {
  font-size: 1.5rem; margin-bottom: 1rem;
}
.cards {
  display: grid;
  grid-template-columns: 1fr;
  gap: 1rem;
}
@media (min-width: 480px) {
  .cards {
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  }
}
.card {
  background: #fff;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 2px 6px rgba(0,0,0,0.1);
}
.card img {
  width: 100%; display: block;
}
.card-body {
  padding: 0.5rem;
}
.card-body h2 {
  font-size: 1rem; margin: 0 0 0.5rem;
}
.card-body .release {
  font-size: 0.85rem; color: #555; margin: 0 0 0.5rem;
}
.card-body .rating {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
.card-body .rating.good { color: #1a7f37; }
.card-body .rating.ok   { color: #d97706; }
.card-body .rating.bad  { color: #c11919; }

@media (prefers-color-scheme: dark) {
  body { background: #111; color: #ddd; }
  .card { background: #222; box-shadow: 0 2px 6px rgba(0,0,0,0.6); }
  .card-body .release { color: #aaa; }
}
"""

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Pathé Den Haag · {date}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>{css}</style>
</head>
<body>
  <h1>🎬 Pathé Den Haag · {date}</h1>
  <div class="cards">
    {cards}
  </div>
  <footer style="margin-top:1rem; font-size:0.75rem; text-align:center;">
    Generated {now} · Source: Pathé API + OMDb
  </footer>
</body>
</html>"""

def rating_class(rating: str) -> str:
    try:
        val = float(rating)
        if val >= 7.5: return "good"
        if val >= 5.0: return "ok"
        return "bad"
    except Exception:
        return ""


def build_html(shows: List[dict], as_of: str) -> str:
    now = time.strftime("%Y-%m-%d %H:%M")
    cards_html: List[str] = []
    for s in shows:
        title   = s.get("title", "")
        release = ", ".join(s.get("releaseAt", []))
        imdb    = s.get("imdbRating", "—")
        cls     = rating_class(imdb)

        # guard against null posterPath
        poster = s.get("posterPath") or {}
        img     = poster.get("md") or poster.get("lg") or ""

        cards_html.append(
            f"<div class='card'>"
            f"  <img src='{img}' alt='Poster for {title}'>"
            f"  <div class='card-body'>"
            f"    <h2>{title}</h2>"
            f"    <p class='release'>{release}</p>"
            f"    <p class='rating {cls}'>{imdb}</p>"
            f"  </div>"
            f"</div>"
        )

    return HTML_TEMPLATE.format(
        date=as_of,
        now=now,
        css=CARD_CSS,
        cards="\n    ".join(cards_html),
    )

# ────────────────────────── CLI / main ─────────────────────
def parse_args() -> argparse.Namespace:
    today = dt.date.today().isoformat()
    p = argparse.ArgumentParser(
        description="Fetch Pathé Den Haag shows and build card-style HTML."
    )
    p.add_argument("--date",     default=today,   metavar="YYYY-MM-DD",
                   help="date to query (default: today)")
    p.add_argument("--imdb-key", help="override OMDb key")
    p.add_argument("--skip-imdb",action="store_true", help="disable IMDb enrichment")
    p.add_argument("--debug",    action="store_true", help="verbose logging")
    p.add_argument("--output",   default="index.html",
                   help="output HTML file (default: index.html)")
    return p.parse_args()

def console_preview(shows: List[dict]) -> None:
    df = pd.DataFrame.from_records([{
        "Title": s["title"],
        "Release": ", ".join(s.get("releaseAt", [])),
        "IMDb": s.get("imdbRating","—")
    } for s in shows])
    try:
        from tabulate import tabulate
        print(tabulate(df, headers="keys", tablefmt="github", showindex=False))
    except ImportError:
        LOG.debug("tabulate not installed – plain text preview")
        print(df.to_string(index=False))

def main() -> None:
    args     = parse_args()
    if args.debug:
        LOG.setLevel(logging.DEBUG)

    imdb_key = args.imdb_key or OMDB_KEY_ENV
    if imdb_key:
        LOG.info("✅ IMDb key loaded from %s",
                 "CLI" if args.imdb_key else "environment")
    else:
        LOG.info("ℹ️  No IMDb key found – skipping ratings")

    LOG.info("🔗  Querying Pathé API for %s …", args.date)
    shows = fetch_shows(args.date)

    if imdb_key and not args.skip_imdb:
        add_imdb_ratings(shows, imdb_key)
    else:
        for s in shows:
            s["imdbRating"] = "—"

    # build the HTML and write it out
    html = build_html(shows, args.date)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(html)
    LOG.info("✅ wrote %s (%d bytes)", args.output, len(html))

    # optional console preview
    console_preview(shows)

if __name__ == "__main__":
    main()

