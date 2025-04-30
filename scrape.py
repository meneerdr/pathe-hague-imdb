#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape.py  –  Pathé The-Hague titles  ➜  pretty mobile-friendly HTML

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
from typing import Dict, List, Optional

import pandas as pd
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

HEADERS: Dict[str, str] = {          # identical to the 100 % working cURL
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

# OMDb / IMDb ----------------------------------------------------------
OMDB_URL = "https://www.omdbapi.com/"
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
def fetch_rating(title: str, key: str) -> Optional[str]:
    try:
        data = get_json(OMDB_URL, params={"apikey": key, "t": title, "r": "json"}, timeout=10)
        rating = data.get("imdbRating")
        if data.get("Response") == "True" and rating and rating != "N/A":
            return rating
    except Exception as exc:  # noqa: BLE001
        LOG.debug("OMDb lookup failed for %s – %s", title, exc)
    return None


def add_imdb_ratings(shows: List[dict], key: str) -> None:
    LOG.info("🔍 fetching IMDb ratings … (max %d threads)", MAX_OMDB_WORKERS)
    with cf.ThreadPoolExecutor(max_workers=MAX_OMDB_WORKERS) as pool:
        futures = {pool.submit(fetch_rating, s["title"], key): s for s in shows}
        for fut in cf.as_completed(futures):
            futures[fut]["imdbRating"] = fut.result() or "—"


# ───────────────────────── HTML output ─────────────────────
MOBILE_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
     margin:1rem;}
h1{font-size:1.5rem;margin:0 0 .75rem 0}
table{width:100%;border-collapse:collapse}
th,td{padding:.5rem .6rem;border-bottom:1px solid #ddd;font-size:.92rem;text-align:left}
th{background:#f5f5f5;font-weight:600}
.rating{font-variant-numeric:tabular-nums}
.rating.good{color:#1a7f37}
.rating.ok{color:#d97706}
.rating.bad{color:#c11919}
@media(prefers-color-scheme:dark){
  body{background:#000;color:#e0e0e0}
  th{background:#111;color:#fff}
  td{border-color:#222}
}
"""

HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Pathé The Hague – {date}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>{css}</style>
</head>
<body>
  <h1>Pathé The Hague · {date}</h1>
  <table>
    <thead><tr><th>Title</th><th>Release</th><th>IMDb</th></tr></thead>
    <tbody>
      {rows}
    </tbody>
  </table>
  <footer style="margin-top:1rem;font-size:.75rem;">
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
    except ValueError:
        return ""  # "—" etc.


def dataframe_to_html(df: pd.DataFrame, as_of: str) -> str:
    rows_html = []
    for _, row in df.iterrows():
        cls = rating_class(row["IMDb"])
        rows_html.append(
            f"<tr>"
            f"<td>{row['Title']}</td>"
            f"<td>{row['Release']}</td>"
            f"<td class='rating {cls}'>{row['IMDb']}</td>"
            f"</tr>"
        )
    return HTML_TEMPLATE.format(date=as_of, now=time.strftime("%Y-%m-%d %H:%M"), css=MOBILE_CSS, rows="\n      ".join(rows_html))


# ────────────────────────── CLI / main ─────────────────────
def parse_args() -> argparse.Namespace:
    today = dt.date.today().isoformat()
    p = argparse.ArgumentParser(description="Fetch Pathé The-Hague shows and build mobile-friendly HTML.")
    p.add_argument("--date", default=today, metavar="YYYY-MM-DD", help="date to query (default: today)")
    p.add_argument("--imdb-key", help="override OMDb key")
    p.add_argument("--skip-imdb", action="store_true", help="disable IMDb enrichment")
    p.add_argument("--debug", action="store_true", help="verbose logging")
    p.add_argument("--output", default="index.html", help="output HTML file (default: index.html)")
    return p.parse_args()


def console_preview(df: pd.DataFrame) -> None:
    """Pretty console preview – Markdown if tabulate is installed."""
    try:
        from tabulate import tabulate  # type: ignore

        print(tabulate(df, headers="keys", tablefmt="github", showindex=False))
    except ImportError:
        LOG.debug("tabulate not installed – falling back to plain text table")
        print(df.to_string(index=False))


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
        add_imdb_ratings(shows, imdb_key)
    else:
        for s in shows:
            s["imdbRating"] = "—"

    df = pd.DataFrame.from_records(
        [
            {
                "Title": s["title"],
                "Release": ", ".join(s.get("releaseAt", [])),
                "IMDb": s.get("imdbRating", "—"),
            }
            for s in shows
        ]
    )

    # Write HTML
    html = dataframe_to_html(df, args.date)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(html)
    LOG.info("✅ wrote %s (%d bytes)", args.output, len(html))

    # Optional quick preview in console
    console_preview(df)

if __name__ == "__main__":
    html = build_html()        # whatever returns the HTML string
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "public/index.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")


