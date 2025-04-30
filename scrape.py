#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape.py  –  Pathé Den Haag shows ➜ mobile-friendly card layout HTML

◆ Requires : requests
◆ Environment
   OMDB_API_KEY or OMDB_KEY – OMDb / IMDb API key
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

import requests
from requests.adapters import HTTPAdapter
from urllib3 import Retry

# ─────────────────────── CONFIG ───────────────────────
PATHÉ_SHOWS_URL = "https://www.pathe.nl/api/shows"
PAGE_SIZES = (100, 50, 20)
DEFAULT_TIMEOUT = 30

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

# ─────────────────────── logging ───────────────────────
LOG = logging.getLogger("pathe")
LOG.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(levelname)-8s %(asctime)s  %(message)s", "%H:%M:%S"))
LOG.addHandler(_handler)

# ───────────────────── HTTP session ─────────────────────
def build_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(HEADERS)
    sess.mount("https://", HTTPAdapter(max_retries=RETRIES))
    return sess

SESSION = build_session()

def get_json(url: str, *, params: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    LOG.debug("GET %s params=%s", url, params)
    resp = SESSION.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

# ──────────────────── Pathé shows ───────────────────────
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

# ──────────────────── OMDb enrichment ───────────────────
def fetch_omdb_data(show: dict, key: str) -> dict:
    # Prefer originalTitle if provided
    title = show.get("originalTitle") or show.get("title")
    # Disambiguate by year: productionYear or first releaseAt
    year = show.get("productionYear")
    if not year:
        dates = show.get("releaseAt") or []
        if dates:
            year = dates[0][:4]
    params: Dict[str, str] = {
        "apikey": key,
        "t": title,
        "type": "movie",
        "r": "json",
    }
    if year:
        params["y"] = str(year)
    try:
        data = get_json(OMDB_URL, params=params, timeout=10)
        if data.get("Response") != "True":
            return {}
        return {
            "imdbRating": data.get("imdbRating") if data.get("imdbRating") not in ("N/A", None) else None,
            "imdbVotes": data.get("imdbVotes") if data.get("imdbVotes") not in ("N/A", None) else None,
            "imdbID": data.get("imdbID"),
            "poster": data.get("Poster") if data.get("Poster") not in ("N/A", None) else None,
        }
    except Exception as exc:
        LOG.warning("OMDb lookup failed for %s – %s", title, exc)
        return {}

def enrich_with_omdb(shows: List[dict], key: str) -> None:
    LOG.info("🔍 fetching OMDb data … (max %d threads)", MAX_OMDB_WORKERS)
    with cf.ThreadPoolExecutor(max_workers=MAX_OMDB_WORKERS) as pool:
        futures = {pool.submit(fetch_omdb_data, show, key): show for show in shows}
        for fut in cf.as_completed(futures):
            show = futures[fut]
            data = fut.result() or {}
            show["omdbRating"] = data.get("imdbRating") or "—"
            show["omdbVotes"]  = data.get("imdbVotes")  or ""
            show["omdbPoster"] = data.get("poster")
            show["imdbID"]     = data.get("imdbID")

# ──────────────────── HTML output ───────────────────────
MOBILE_CSS = """
body{margin:1rem;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
h1{font-size:1.5rem;margin:0 0 1rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));grid-gap:0.5rem}
.card{display:block;border:1px solid #ddd;border-radius:8px;overflow:hidden;text-decoration:none;color:inherit;background:#fff}
.card img{width:100%;display:block}
.card-no-image{width:100%;padding-top:150%;background:#eee;display:flex;align-items:center;justify-content:center;color:#666;font-size:.8rem}
.card-body{padding:.5rem}
.card-title{font-size:1rem;line-height:1.2;margin:0}
.card-date{font-size:.85rem;margin:.25rem 0}
.rating{font-variant-numeric:tabular-nums;font-size:.95rem;margin:0}
.rating strong{font-weight:bold}
.rating small{display:block;font-size:.75rem;color:#666}
@media(prefers-color-scheme:dark){
  body{background:#000;color:#e0e0e0}
  .card{background:#111;border-color:#222}
  .rating small{color:#888}
}
"""

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>🎬 Pathé Den Haag · {date}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>{css}</style>
</head>
<body>
  <h1>🎬 Pathé Den Haag · {date}</h1>
  <div class="grid">
    {cards}
  </div>
  <footer style="margin-top:1rem;font-size:.75rem;">
    Generated {now} · Source: Pathé API + OMDb
  </footer>
</body>
</html>
"""

def build_html(shows: List[dict], date: str) -> str:
    cards: List[str] = []
    for show in shows:
        # Poster fallback: Pathé → OMDb → none
        poster_md = (show.get("posterPath") or {}).get("md")
        img_src   = poster_md or show.get("omdbPoster") or ""
        if img_src:
            img_tag = f'<img src="{img_src}" alt="{show.get("title","")} poster">'
        else:
            img_tag = '<div class="card-no-image">No image</div>'

        # Title / date
        title    = show.get("title","")
        date_str = ", ".join(show.get("releaseAt", []))

        # Rating + votes
        rating  = show.get("omdbRating","—")
        votes   = show.get("omdbVotes","")
        # convert OMDb comma-grouping to dot-grouping
        votes_display = votes.replace(",",".") if votes else ""
        rating_html = f'<div class="rating"><strong>{rating}</strong>'
        if votes_display:
            rating_html += f'<br><small>{votes_display} votes</small>'
        rating_html += "</div>"

        # IMDB link
        imdb_id = show.get("imdbID")
        href    = f"https://www.imdb.com/title/{imdb_id}" if imdb_id else "#"

        card = (
            f'<a class="card" href="{href}" target="_blank">'
            f'{img_tag}'
            f'<div class="card-body">'
            f'<h2 class="card-title">{title}</h2>'
            f'<div class="card-date">{date_str}</div>'
            f'{rating_html}'
            f'</div>'
            f'</a>'
        )
        cards.append(card)

    now = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    return HTML_TEMPLATE.format(date=date, css=MOBILE_CSS, cards="\n    ".join(cards), now=now)

# ────────────────────── CLI / main ─────────────────────
def parse_args() -> argparse.Namespace:
    today = dt.date.today().isoformat()
    p = argparse.ArgumentParser(description="Fetch Pathé Den Haag shows → HTML cards")
    p.add_argument("--date", default=today, metavar="YYYY-MM-DD",
                   help="which date to query (default: today)")
    p.add_argument("--imdb-key", help="override OMDb API key")
    p.add_argument("--skip-imdb", action="store_true",
                   help="do not enrich with OMDb data")
    p.add_argument("--output", default="index.html",
                   help="output HTML file path")
    p.add_argument("--debug", action="store_true",
                   help="enable debug logging")
    return p.parse_args()

def main() -> None:
    args = parse_args()
    if args.debug:
        LOG.setLevel(logging.DEBUG)

    imdb_key = args.imdb_key or OMDB_KEY_ENV
    if imdb_key and not args.skip_imdb:
        LOG.info("✅ OMDb key loaded from %s", "CLI" if args.imdb_key else "environment")
    else:
        LOG.info("ℹ️  OMDb enrichment disabled or key missing")

    LOG.info("🔗 Querying Pathé API for %s …", args.date)
    shows = fetch_shows(args.date)

    if imdb_key and not args.skip_imdb:
        enrich_with_omdb(shows, imdb_key)
    else:
        # stub out empty OMDb fields
        for show in shows:
            show["omdbRating"] = "—"
            show["omdbVotes"]  = ""
            show["omdbPoster"] = None
            show["imdbID"]     = None

    html = build_html(shows, args.date)

    # make sure output dir exists
    outdir = os.path.dirname(args.output)
    if outdir:
        os.makedirs(outdir, exist_ok=True)

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(html)
    LOG.info("✅ wrote %s (%d bytes)", args.output, len(html))

if __name__ == "__main__":
    main()

