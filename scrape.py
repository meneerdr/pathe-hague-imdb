#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zone.py  –  Pathé Den Haag shows ➜ mobile-friendly card layout HTML
    (MVP + Den Haag-zone filtering)

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
from typing import Dict, List, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3 import Retry

# ─────────────────────── CONFIG ───────────────────────
PATHÉ_SHOWS_URL   = "https://www.pathe.nl/api/shows"
ZONE_URL_TEMPLATE = "https://www.pathe.nl/api/zone/den-haag"
OMDB_URL          = "https://www.omdbapi.com/"

PAGE_SIZES      = (100, 50, 20)
DEFAULT_TIMEOUT = 30
MAX_OMDB_WORKERS = 10

RETRIES = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=2,
    status_forcelist=(500,502,503,504),
    allowed_methods={"GET"},
)

HEADERS: Dict[str,str] = {
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

OMDB_KEY_ENV = os.getenv("OMDB_API_KEY") or os.getenv("OMDB_KEY")

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
    sess.mount("https://", HTTPAdapter(max_retries=RETRIES, pool_connections=4, pool_maxsize=4))
    return sess

SESSION = build_session()

def get_json(url: str, *, params: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    LOG.debug("GET %s params=%s", url, params)
    resp = SESSION.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

# ──────────────────── Pathé shows ───────────────────────
def fetch_shows(date: str) -> List[dict]:
    """Fetch all shows for a given date, trying multiple pageSizes."""
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

# ────────────────── Den Haag zone ───────────────────────
def fetch_zone_slugs() -> set[str]:
    """Fetch all slugs playing in the Den Haag zone today."""
    data = get_json(ZONE_URL_TEMPLATE, params={"language": "nl"})
    zone_shows = data.get("shows", [])
    slugs = {s["slug"] for s in zone_shows if "slug" in s}
    LOG.info("· fetched %d Den Haag zone slugs", len(slugs))
    return slugs

# ──────────────────── OMDb enrichment ───────────────────
def fetch_omdb_data(show: dict, key: str) -> dict:
    title = show.get("originalTitle") or show.get("title")
    year  = show.get("productionYear") or (show.get("releaseAt") or [""])[0][:4]
    params: Dict[str,str] = {"apikey": key, "t": title, "type": "movie", "r": "json"}
    if year:
        params["y"] = year
    try:
        data = get_json(OMDB_URL, params=params, timeout=10)
        if data.get("Response") != "True":
            return {}
        # parse Rotten Tomatoes & Metacritic
        rt = mc = None
        for r in data.get("Ratings", []):
            src, val = r.get("Source"), r.get("Value")
            if src=="Rotten Tomatoes" and val and val!="N/A":
                rt = val.rstrip("%")
            if src=="Metacritic" and val and val!="N/A":
                mc = val.split("/")[0]
        return {
            "imdbRating": data.get("imdbRating") if data.get("imdbRating") not in ("N/A",None) else None,
            "imdbVotes":  data.get("imdbVotes")  if data.get("imdbVotes")  not in ("N/A",None) else None,
            "imdbID":     data.get("imdbID"),
            "omdbPoster": data.get("Poster")      if data.get("Poster")      not in ("N/A",None) else None,
            "rtRating":   rt,
            "mcRating":   mc,
        }
    except Exception as exc:
        LOG.warning("OMDb lookup failed for %s – %s", title, exc)
        return {}

def enrich_with_omdb(shows: List[dict], key: str) -> None:
    LOG.info("🔍 fetching OMDb data … (max %d threads)", MAX_OMDB_WORKERS)
    with cf.ThreadPoolExecutor(max_workers=MAX_OMDB_WORKERS) as pool:
        futures = {pool.submit(fetch_omdb_data, s, key): s for s in shows}
        for fut in cf.as_completed(futures):
            s    = futures[fut]
            data = fut.result() or {}
            s["imdbRating"] = data.get("imdbRating")
            s["imdbVotes"]  = data.get("imdbVotes")
            s["omdbPoster"] = data.get("omdbPoster")
            s["imdbID"]     = data.get("imdbID")
            s["rtRating"]   = data.get("rtRating")
            s["mcRating"]   = data.get("mcRating")

# ─────────────────── rating classes ────────────────────
def imdb_class(r: Optional[str]) -> str:
    try:
        v = float(r) if r else 0
        if v >= 7.0: return "good"
        if v >= 6.0: return "ok"
        if v >  0:   return "bad"
    except: pass
    return ""

def rt_class(r: Optional[str]) -> str:
    try:
        v = int(r) if r else 0
        if v >= 75: return "good"
        if v >= 50: return "ok"
        if v >  0:  return "bad"
    except: pass
    return ""

def mc_class(r: Optional[str]) -> str:
    try:
        v = int(r) if r else 0
        if v >= 75: return "good"
        if v >= 50: return "ok"
        if v >  0:  return "bad"
    except: pass
    return ""

# ──────────────────── HTML output ───────────────────────
MOBILE_CSS = """
body{margin:1rem;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
h1{font-size:1.5rem;margin:0 0 1rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));grid-gap:.5rem}
.card{display:block;border:1px solid #ddd;border-radius:8px;overflow:hidden;text-decoration:none;color:inherit;background:#fff}
.card img{width:100%;display:block}
.card-no-image{width:100%;padding-top:150%;background:#eee;display:flex;align-items:center;justify-content:center;color:#666;font-size:.8rem}
.card-body{padding:.5rem}
.card-title{font-size:1rem;line-height:1.2;margin:0}
.card-date{font-size:.85rem;margin:.25rem 0}
.ratings-inline {
  display:grid;grid-template-columns:1fr 1fr 1fr;gap:.4rem;
  font-size:.95rem;color:#555;font-variant-numeric:tabular-nums;
}
.ratings-inline span{text-align:center}
.ratings-inline .imdb.good {color:#1a7f37;font-weight:bold}
.ratings-inline .imdb.ok   {color:#d97706;font-weight:bold}
.ratings-inline .imdb.bad  {color:#c11919;font-weight:bold}
.ratings-inline .rt.good, .ratings-inline .mc.good {color:#1a7f37}
.ratings-inline .rt.ok,   .ratings-inline .mc.ok   {color:#d97706}
.ratings-inline .rt.bad,  .ratings-inline .mc.bad  {color:#c11919}
@media(prefers-color-scheme:dark){
  body{background:#000;color:#e0e0e0}
  .card{background:#111;border-color:#222}
  .ratings-inline {color:#ccc}
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
    for s in shows:
        # poster fallback Pathé → OMDb → none
        pm = (s.get("posterPath") or {}).get("md")
        img = pm or s.get("omdbPoster") or ""
        img_tag = f'<img src="{img}" alt="{s.get("title","")}">' if img else '<div class="card-no-image">No image</div>'

        title = s.get("title","")
        date_str = ", ".join(s.get("releaseAt", []))

        imdb = s.get("imdbRating") or ""
        rt   = s.get("rtRating")  or ""
        mc   = s.get("mcRating")  or ""

        spans = [
            f'<span class="imdb {imdb_class(imdb)}">' + (f'<strong>{imdb}</strong>' if imdb else "") + "</span>",
            f'<span class="rt {rt_class(rt)}">'   + rt   + "</span>",
            f'<span class="mc {mc_class(mc)}">'   + mc   + "</span>",
        ]
        ratings = f'<div class="ratings-inline">{"".join(spans)}</div>'

        href = f'https://www.imdb.com/title/{s.get("imdbID")}' if s.get("imdbID") else "#"

        cards.append(
            f'<a class="card" href="{href}" target="_blank">'
            f'{img_tag}'
            f'<div class="card-body">'
            f'<h2 class="card-title">{title}</h2>'
            f'<div class="card-date">{date_str}</div>'
            f'{ratings}'
            f'</div></a>'
        )

    now = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    return HTML_TEMPLATE.format(date=date, css=MOBILE_CSS, cards="\n    ".join(cards), now=now)

# ────────────────────── CLI / main ─────────────────────
def parse_args() -> argparse.Namespace:
    today = dt.date.today().isoformat()
    p = argparse.ArgumentParser(description="Fetch Pathé Den Haag shows → HTML cards")
    p.add_argument("--date", default=today, help="YYYY-MM-DD (default: today)")
    p.add_argument("--imdb-key", help="override OMDb API key")
    p.add_argument("--skip-imdb", action="store_true", help="disable OMDb enrichment")
    p.add_argument("--output", default="index.html", help="output file")
    p.add_argument("--debug", action="store_true", help="verbose logging")
    return p.parse_args()

def main() -> None:
    args = parse_args()
    if args.debug:
        LOG.setLevel(logging.DEBUG)

    key = args.imdb_key or OMDB_KEY_ENV
    LOG.info("🔗 Querying Pathé API for %s …", args.date)
    shows      = fetch_shows(args.date)
    zone_slugs = fetch_zone_slugs()

    # keep only shows whose slug is in the Den Haag zone
    filtered = [s for s in shows if s.get("slug") in zone_slugs]
    LOG.info("· %d shows remain after zone filter", len(filtered))

    if key and not args.skip_imdb:
        enrich_with_omdb(filtered, key)
    else:
        for s in filtered:
            s["imdbRating"] = None
            s["imdbVotes"]  = None
            s["omdbPoster"] = None
            s["imdbID"]     = None
            s["rtRating"]   = None
            s["mcRating"]   = None

    html = build_html(filtered, args.date)
    outdir = os.path.dirname(args.output)
    if outdir:
        os.makedirs(outdir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(html)
    LOG.info("✅ wrote %s (%d bytes)", args.output, len(html))

if __name__ == "__main__":
    main()

