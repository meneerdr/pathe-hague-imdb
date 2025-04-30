#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape.py  –  Pathé The-Hague titles  ➜  pretty mobile-friendly HTML cards

◆ Requires : requests  pandas
◆ Optional : tabulate  (only for console preview)

Environment
-----------
OMDB_KEY or OMDB_API_KEY   –  your OMDb / IMDb API key
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import logging
import os
import sys
import time
from typing import Dict, List

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3 import Retry

# ───────────────────────── CONFIG ──────────────────────────
PATHÉ_SHOWS_URL = "https://www.pathe.nl/api/shows"
PAGE_SIZES = (100, 50, 20)
DEFAULT_TIMEOUT = 30  # seconds

RETRIES = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=2,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods={"GET"},
)

HEADERS: Dict[str, str] = {
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

OMDB_URL = "https://www.omdbapi.com/"
OMDB_KEY_ENV = os.getenv("OMDB_API_KEY") or os.getenv("OMDB_KEY")
MAX_OMDB_WORKERS = 10

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
            data = get_json(PATHÉ_SHOWS_URL, params={"language": "nl", "date": date, "pageSize": size})
            shows = data.get("shows", [])
            LOG.info("· got %d shows (pageSize=%d)", len(shows), size)
            if shows:
                return shows
        except Exception as exc:
            LOG.warning("pageSize=%d failed – %s", size, exc)
    LOG.critical("💥 Could not retrieve any shows for %s", date)
    sys.exit(1)

# ─────────────────────── IMDb enrichment ───────────────────
def fetch_imdb_data(title: str, key: str) -> dict:
    out = {"imdbRating": None, "imdbVotes": None, "imdbPoster": None, "imdbID": None}
    try:
        data = get_json(OMDB_URL, params={"apikey": key, "t": title, "r": "json"}, timeout=10)
        if data.get("Response") == "True":
            if (r := data.get("imdbRating")) not in (None, "N/A"):
                out["imdbRating"] = r
            if (v := data.get("imdbVotes")) not in (None, "N/A"):
                out["imdbVotes"] = v
            if (p := data.get("Poster")) not in (None, "N/A"):
                out["imdbPoster"] = p
            if (i := data.get("imdbID")):
                out["imdbID"] = i
    except Exception:
        LOG.debug("OMDb lookup failed for %s", title, exc_info=True)
    return out

def add_imdb_data(shows: List[dict], key: str) -> None:
    LOG.info("🔍 fetching IMDb data … (max %d threads)", MAX_OMDB_WORKERS)
    with cf.ThreadPoolExecutor(max_workers=MAX_OMDB_WORKERS) as pool:
        futures = {pool.submit(fetch_imdb_data, s["title"], key): s for s in shows}
        for fut in cf.as_completed(futures):
            data = fut.result()
            show = futures[fut]
            show["imdbRating"] = data["imdbRating"] or "—"
            show["imdbVotes"]  = data["imdbVotes"]  or ""
            show["imdbPoster"] = data["imdbPoster"] or ""
            show["imdbID"]     = data["imdbID"]     or ""

# ────────────────────────── CSS + HTML ────────────────────
MOBILE_CSS = """
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  margin: 1rem;
}
h1 {
  font-size: 1.5rem;
  margin-bottom: 1rem;
}
h1 a {
  margin-left: .5rem;
  font-size: .875rem;
  text-decoration: none;
  color: #0366d6;
}
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px,1fr));
  gap: 1rem;
}
.card {
  background: #fff;
  border-radius: .5rem;
  box-shadow: 0 1px 3px rgba(0,0,0,0.1);
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.card img {
  width: 100%;
  display: block;
}
.card h2 {
  font-size: 1rem;
  margin: .25rem .5rem 0;
}
.card time {
  margin: .1rem .5rem .25rem;
  color: #666;
  font-size: .875rem;
}
.rating {
  margin: .1rem .5rem 0;
  font-size: .875rem;
}
.rating.good { color: #1a7f37; }
.rating.ok   { color: #d97706; }
.rating.bad  { color: #c11919; }
.rating strong { font-weight: bold; }
.votes {
  margin: .05rem .5rem .5rem;
  font-size: .75rem;
  color: #444;
}
@media(prefers-color-scheme:dark) {
  body { background: #000; color: #e0e0e0; }
  .card { background: #111; }
  .card time { color: #aaa; }
}
"""

def rating_class(rating: str) -> str:
    try:
        val = float(rating)
        if val >= 7.5:
            return "good"
        if val >= 5.0:
            return "ok"
        return "bad"
    except ValueError:
        return ""

def build_html(shows: List[dict], date: str) -> str:
    now = time.strftime("%Y-%m-%d %H:%M")
    header = f'<h1>🎬 Pathé Den Haag · {date}<a href="#unrated">· Unrated</a></h1>'
    cards_html: List[str] = []
    anchor_added = False
    placeholder = "https://via.placeholder.com/300x450?text=No+Poster"

    for s in shows:
        title   = s["title"]
        release = ", ".join(s.get("releaseAt", []))

        # pick Pathé poster, else OMDb fallback, else placeholder
        pp = s.get("posterPath")
        poster_md = pp.get("md") if isinstance(pp, dict) else None
        poster = poster_md or s.get("imdbPoster") or placeholder

        imdb    = s.get("imdbRating", "—")
        votes   = s.get("imdbVotes", "").replace(",", ".")
        imdb_id = s.get("imdbID", "")
        imdb_url = f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else "#"

        anchor = ''
        if imdb == "—" and not anchor_added:
            anchor = ' id="unrated"'
            anchor_added = True

        rating_html = f'<div class="rating {rating_class(imdb)}"><strong>{imdb}</strong></div>'
        votes_html  = f'<div class="votes">{votes} votes</div>' if votes else ""

        cards_html.append(f"""
      <div class="card"{anchor}>
        <a href="{imdb_url}" target="_blank" rel="noopener">
          <img src="{poster}" alt="{title} poster" loading="lazy"/>
        </a>
        <h2>{title}</h2>
        <time datetime="{release}">{release}</time>
        {rating_html}
        {votes_html}
      </div>
        """)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Pathé Den Haag – {date}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>{MOBILE_CSS}</style>
</head>
<body>
  {header}
  <div class="cards">
{''.join(cards_html)}
  </div>
  <footer style="margin-top:1rem;font-size:.75rem;">
    Generated {now} · Source: Pathé API + OMDb
  </footer>
</body>
</html>
"""

# ────────────────────────── CLI / main ─────────────────────
def parse_args() -> argparse.Namespace:
    today = dt.date.today().isoformat()
    p = argparse.ArgumentParser(description="Fetch Pathé Den Haag shows and build HTML cards.")
    p.add_argument("--date",     default=today,      metavar="YYYY-MM-DD", help="date to query (default: today)")
    p.add_argument("--imdb-key", help="override OMDb key")
    p.add_argument("--skip-imdb",action="store_true", help="disable IMDb enrichment")
    p.add_argument("--debug",    action="store_true", help="verbose logging")
    p.add_argument("--output",   default="public/index.html", help="output HTML file")
    return p.parse_args()

def main() -> None:
    args = parse_args()
    if args.debug:
        LOG.setLevel(logging.DEBUG)

    imdb_key = args.imdb_key or OMDB_KEY_ENV
    if imdb_key:
        LOG.info("✅ IMDb key loaded from %s", "CLI" if args.imdb_key else "environment")
    else:
        LOG.info("ℹ️  No IMDb key found – ratings will be skipped")

    LOG.info("🔗  Querying Pathé JSON API for %s …", args.date)
    shows = fetch_shows(args.date)

    if imdb_key and not args.skip_imdb:
        add_imdb_data(shows, imdb_key)
    else:
        for s in shows:
            s["imdbRating"] = "—"
            s["imdbVotes"]  = ""
            s["imdbPoster"] = ""
            s["imdbID"]     = ""

    # ── **NEW**: sort by IMDb rating desc, unrated (“—”) forced to bottom
    def _sort_key(s: dict) -> float:
        r = s.get("imdbRating", "—")
        try:
            return float(r)
        except ValueError:
            return -1.0
    shows.sort(key=_sort_key, reverse=True)

    html = build_html(shows, args.date)

    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(html)
    LOG.info("✅ wrote %s (%d bytes)", args.output, len(html))

if __name__ == "__main__":
    main()

