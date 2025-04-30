#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape.py  –  Pathé The-Hague titles  ➜  pretty mobile-friendly card grid

◆ Requires : requests  pandas
◆ Optional : tabulate  (only for console preview)

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
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3 import Retry

# ───────────────────────── CONFIG ──────────────────────────
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
def fetch_rating_and_id(title: str, key: str) -> Tuple[Optional[str], str]:
    """
    Returns (rating, imdbID) or (None, "") on failure.
    """
    try:
        data = get_json(
            OMDB_URL,
            params={"apikey": key, "t": title, "r": "json"},
            timeout=10,
        )
        if data.get("Response") == "True":
            rating = data.get("imdbRating")
            imdbid = data.get("imdbID", "")
            if rating and rating != "N/A":
                return rating, imdbid
    except Exception as exc:
        LOG.debug("OMDb lookup failed for %s – %s", title, exc)
    return None, ""


def add_imdb_details(shows: List[dict], key: str) -> None:
    LOG.info("🔍 fetching IMDb ratings & IDs … (max %d threads)", MAX_OMDB_WORKERS)
    with cf.ThreadPoolExecutor(max_workers=MAX_OMDB_WORKERS) as pool:
        futures = {pool.submit(fetch_rating_and_id, s["title"], key): s for s in shows}
        for fut in cf.as_completed(futures):
            show = futures[fut]
            rating, imdbid = fut.result()
            show["imdbRating"] = rating or "—"
            show["imdbID"] = imdbid or ""


# ───────────────────────── HTML + CSS ─────────────────────
MOBILE_CSS = """
:root{--gap:.75rem;--card-bg:#fff;--card-shadow:rgba(0,0,0,0.1);}
body {
  margin:1rem;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
h1 {
  font-size:1.5rem;
  margin:0 0 1rem;
  display:flex;
  align-items:center;
}
h1::before { content:"🎬"; margin-right:.5rem; }
.grid {
  display:grid;
  gap:var(--gap);
  grid-template-columns: repeat(auto-fill, minmax(140px,1fr));
}
.card {
  background:var(--card-bg);
  border-radius:8px;
  box-shadow:0 1px 3px var(--card-shadow);
  overflow:hidden;
  display:flex;
  flex-direction:column;
}
.card img {
  width:100%;
  display:block;
  aspect-ratio:2/3;
  object-fit:cover;
}
.info {
  padding:.5rem;
  flex:1;
  display:flex;
  flex-direction:column;
  justify-content:space-between;
}
.info h2 {
  font-size:1rem;
  margin:0 0 .5rem;
}
.release { font-size:.85rem; color:#555; margin:0 0 .5rem; }
.rating {
  font-variant-numeric:tabular-nums;
  font-weight:600;
}
.rating.good { color:#1a7f37; }
.rating.ok   { color:#d97706; }
.rating.bad  { color:#c11919; }
footer {
  margin-top:1rem;
  font-size:.75rem;
  text-align:center;
  color:#666;
}
@media (prefers-color-scheme:dark) {
  body { background:#000; color:#e0e0e0; }
  .card { background:#111; box-shadow:0 1px 3px rgba(0,0,0,0.7); }
  .release { color:#aaa; }
  footer { color:#888; }
}
"""

HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Pathé Den Haag · {date}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>{css}</style>
</head>
<body>
  <h1>Pathé Den Haag · {date}</h1>
  <div class="grid">
    {cards}
  </div>
  <footer>
    Generated {now} · Source: Pathé API + OMDb
  </footer>
</body>
</html>
"""


def rating_class(rating: str) -> str:
    try:
        val = float(rating)
        if val >= 7.5:
            return "good"
        if val >= 5.0:
            return "ok"
        return "bad"
    except Exception:
        return ""


def build_html(shows: List[dict], date: str) -> str:
    # Sort by numeric rating desc, unranked ("—") last
    def sort_key(s: dict) -> float:
        r = s.get("imdbRating", "—")
        try:
            return float(r)
        except Exception:
            return -1.0

    shows = sorted(shows, key=sort_key, reverse=True)

    cards: list[str] = []
    for s in shows:
        title     = s["title"]
        release   = ", ".join(s.get("releaseAt", []))
        rating    = s.get("imdbRating", "—")
        imdbid    = s.get("imdbID", "")
        poster_md = (s.get("posterPath") or {}).get("md", "")
        link      = f"https://www.imdb.com/title/{imdbid}/" if imdbid else "#"
        cls       = rating_class(rating)

        cards.append(
            f"<div class='card'>"
            f"<a href='{link}' target='_blank' rel='noopener'>"
            f"<img src='{poster_md}' alt='{title} poster'/>"
            f"</a>"
            f"<div class='info'>"
            f"<h2>{title}</h2>"
            f"<p class='release'>{release}</p>"
            f"<p class='rating {cls}'>{rating}</p>"
            f"</div>"
            f"</div>"
        )

    now = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    return HTML_TEMPLATE.format(date=date, now=now, css=MOBILE_CSS, cards="\n    ".join(cards))


# ────────────────────────── CLI / main ─────────────────────
def parse_args() -> argparse.Namespace:
    today = dt.date.today().isoformat()
    p = argparse.ArgumentParser(description="Fetch Pathé Den Haag shows → mobile card grid HTML")
    p.add_argument("--date", default=today, metavar="YYYY-MM-DD", help="date to query (default: today)")
    p.add_argument("--imdb-key", help="override OMDb key")
    p.add_argument("--skip-imdb", action="store_true", help="disable IMDb enrichment")
    p.add_argument("--debug", action="store_true", help="verbose logging")
    p.add_argument("--output", default="index.html", help="output HTML file (default: index.html)")
    return p.parse_args()


def console_preview(shows: List[dict]) -> None:
    """Fallback console preview via pandas + tabulate."""
    df = pd.DataFrame.from_records(
        [{"Title": s["title"], "Release": ", ".join(s.get("releaseAt", [])), "IMDb": s.get("imdbRating", "—")}
         for s in shows]
    )
    try:
        from tabulate import tabulate  # type: ignore
        print(tabulate(df, headers="keys", tablefmt="github", showindex=False))
    except ImportError:
        LOG.debug("tabulate not installed – showing plain table")
        print(df.to_string(index=False))


def main() -> None:
    args = parse_args()
    if args.debug:
        LOG.setLevel(logging.DEBUG)

    imdb_key = args.imdb_key or OMDB_KEY_ENV
    if imdb_key:
        LOG.info("✅ IMDb key from %s", "CLI" if args.imdb_key else "env")
    else:
        LOG.info("ℹ️  No IMDb key found, skipping ratings/links")

    LOG.info("🔗  Querying Pathé JSON API for %s …", args.date)
    shows = fetch_shows(args.date)

    if imdb_key and not args.skip_imdb:
        add_imdb_details(shows, imdb_key)
    else:
        for s in shows:
            s["imdbRating"] = "—"
            s["imdbID"] = ""

    LOG.info("✅  Building HTML …")
    html = build_html(shows, args.date)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(html)
    LOG.info("✅  Wrote %s (%d bytes)", args.output, len(html))

    console_preview(shows)


if __name__ == "__main__":
    main()

