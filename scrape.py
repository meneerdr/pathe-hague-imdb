#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape.py  –  Pathé Den Haag shows ➜ mobile-friendly card layout HTML
              • zone-filtered to Den Haag
              • marks Ypenburg / Spuimarkt / Buitenhof presence
              • enriches with OMDb (IMDb/RT/MC)
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import logging
import os
import sys
import time
from typing import Dict, List, Optional, Set
from concurrent.futures import ThreadPoolExecutor, as_completed
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3 import Retry

# ─────────────────────── CONFIG ───────────────────────
PATHÉ_SHOWS_URL = "https://www.pathe.nl/api/shows"
ZONE_URL         = "https://www.pathe.nl/api/zone/den-haag"
CINEMA_URL_TMPL  = "https://www.pathe.nl/api/cinema/{slug}/shows"

PAGE_SIZES       = (100, 50, 20)
DEFAULT_TIMEOUT  = 30

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

OMDB_URL      = "https://www.omdbapi.com/"
OMDB_KEY_ENV  = os.getenv("OMDB_API_KEY") or os.getenv("OMDB_KEY")
MAX_OMDB_WORKERS = 10

LEAK_CHECK_WORKERS = 10

FAV_CINEMAS = [
    ("pathe-ypenburg",   "Ypenburg"),
    ("pathe-spuimarkt",  "Spui"),
    ("pathe-buitenhof",  "Buitenhof"),
]

# ─────────────────────── logging ───────────────────────
LOG = logging.getLogger("pathe")
LOG.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(levelname)-8s %(asctime)s  %(message)s","%H:%M:%S"))
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
    r = SESSION.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ──────────────────── Pathé shows ───────────────────────
def fetch_shows(date: str) -> List[dict]:
    for size in PAGE_SIZES:
        try:
            data = get_json(PATHÉ_SHOWS_URL, params={"language":"nl","date":date,"pageSize":size})
            shows = data.get("shows",[])
            LOG.info("· got %d shows (pageSize=%d)", len(shows), size)
            if shows:
                return shows
        except Exception as exc:
            LOG.warning("pageSize=%d failed – %s", size, exc)
    LOG.critical("💥 Could not retrieve any shows for %s", date)
    sys.exit(1)

def fetch_zone_slugs() -> Set[str]:
    LOG.info("🔗 fetching Den Haag zone …")
    data = get_json(ZONE_URL, params={"language":"nl"})
    arr = data.get("shows",[])
    slugs = {s["slug"] for s in arr if "slug" in s}
    LOG.info("· got %d zone slugs", len(slugs))
    return slugs

def fetch_cinema_slugs(cinema_slug: str, date: str) -> Set[str]:
    url = CINEMA_URL_TMPL.format(slug=cinema_slug)
    LOG.info("🔗 fetching cinema '%s' …", cinema_slug)
    data = get_json(url, params={"language":"nl","date":date})
    shows_obj = data.get("shows",{})
    if isinstance(shows_obj, dict):
        slugs = set(shows_obj.keys())
        LOG.info("· got %d slugs for %s", len(slugs), cinema_slug)
        return slugs
    LOG.warning("· unexpected .shows for %s: %r", cinema_slug, shows_obj)
    return set()

def fetch_zone_shows() -> dict:
    """ Fetch shows from the Den Haag zone to retrieve kids data and other info """
    LOG.info("🔗 Fetching Den Haag zone shows …")
    data = get_json(ZONE_URL, params={"language":"nl"})
    shows = {s["slug"]: s for s in data.get("shows", []) if "slug" in s}
    LOG.info("· Got %d zone shows", len(shows))
    return shows

def fetch_zone_data() -> Dict[str, dict]:
    """ Fetch kids + bookable data from the Den Haag zone """
    LOG.info("🔗 Fetching zone data for Den Haag …")
    data = get_json(ZONE_URL, params={"language":"nl"})
    zone_data = {}
    for show in data.get("shows", []):
        zone_data[show["slug"]] = {
            "isKids":   show.get("isKids", False),
            "bookable": show.get("bookable", False),
        }
    return zone_data

# ──────────────────── OMDb enrichment ───────────────────
def fetch_omdb_data(show: dict, key: str) -> dict:
    # Prefer originalTitle if available
    title = show.get("originalTitle") or show.get("title","")
    # Disambiguate by year
    year = show.get("productionYear")
    if not year:
        dates = show.get("releaseAt") or []
        if dates:
            year = dates[0][:4]
    params: Dict[str,str] = {"apikey":key,"t":title,"type":"movie","r":"json"}
    if year:
        params["y"] = str(year)
    try:
        d = get_json(OMDB_URL, params=params, timeout=10)
        if d.get("Response") != "True":
            return {}
        # collect RT & MC
        rt = mc = None
        for r in d.get("Ratings",[]):
            if r.get("Source") == "Rotten Tomatoes" and r.get("Value") not in ("N/A", None):
                rt = r["Value"]
            if r.get("Source") == "Metacritic" and r.get("Value") not in ("N/A", None):
                mc = r["Value"].split("/")[0]

        return {
            "imdbRating": d.get("imdbRating") if d.get("imdbRating") not in ("N/A", None) else None,
            "imdbVotes": d.get("imdbVotes") if d.get("imdbVotes") not in ("N/A", None) else None,
            "imdbID": d.get("imdbID"),
            "omdbPoster": d.get("Poster") if d.get("Poster") not in ("N/A", None) else None,
            "rtRating": rt,
            "mcRating": mc,
            "runtime": d.get("Runtime")  # Ensure runtime is fetched
        }
    except Exception as e:
        LOG.warning("OMDb lookup failed for %s – %s", title, e)
        return {}


def enrich_with_omdb(shows: List[dict], key: str) -> None:
    LOG.info("🔍 fetching OMDb data … (max %d threads)", MAX_OMDB_WORKERS)
    with cf.ThreadPoolExecutor(max_workers=MAX_OMDB_WORKERS) as PX:
        futs = {PX.submit(fetch_omdb_data, s, key): s for s in shows}
        for fut in cf.as_completed(futs):
            s = futs[fut]
            data = fut.result() or {}
            s["omdbRating"] = data.get("imdbRating")
            s["omdbVotes"] = data.get("imdbVotes")
            s["imdbID"] = data.get("imdbID")
            s["omdbPoster"] = data.get("omdbPoster")
            s["rtRating"] = data.get("rtRating")
            s["mcRating"] = data.get("mcRating")
            s["runtime"] = data.get("runtime")  # Ensure the runtime field is populated here


# ──────────────────── Leak detection ───────────────────
def check_leak(imdb_id: str) -> bool:
    """
    Returns True if apibay returns any vip‐status hits whose
    name contains BrRip, BDRip, BluRay, WEBRip, WEB-DL, WEB or HDRip.
    """
    try:
        data = get_json(
            "https://apibay.org/q.php",
            params={"q": imdb_id, "cat": ""},
            timeout=5
        )
        for t in data:
            if t.get("status") == "vip" and re.search(
                r"(BrRip|BDRip|BluRay|WEBRip|WEB-DL|WEB|HDRip)",
                t.get("name", ""), re.I
            ):
                return True
    except Exception:
        pass
    return False


# ──────────────────── rating classes ───────────────────
def cls_imdb(r: Optional[str]) -> str:
    try:
        v = float(r) if r else 0
        if v>=7:  return "good"
        if v>=6:  return "ok"
        if v>0:   return "bad"
    except: pass
    return ""

def cls_rt(r: Optional[str]) -> str:
    try:
        v = int(r.rstrip("%")) if r else 0
        if v>=75: return "good"
        if v>=50: return "ok"
        if v>0:   return "bad"
    except: pass
    return ""

def cls_mc(r: Optional[str]) -> str:
    try:
        v = int(r) if r else 0
        if v>=75: return "good"
        if v>=50: return "ok"
        if v>0:   return "bad"
    except: pass
    return ""


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

/* Ratings Inline (logos aligned above the ratings) */
.ratings-inline {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: .4rem;
  font-size: .95rem;
  color: #555;
  font-variant-numeric: tabular-nums;
  text-align: center;
  margin-bottom: 0.3rem;
  margin-top: 0.3rem;
}

.rating-block {
  display: flex;
  flex-direction: column;
  align-items: center;
}

.ratings-inline img {
  height: 1rem;
  margin-bottom: 0.2rem; /* Space between logo and rating */
  vertical-align: middle;
}
.ratings-inline .imdb.good {color:#1a7f37;font-weight:bold}
.ratings-inline .imdb.ok   {color:#d97706;font-weight:bold}
.ratings-inline .imdb.bad  {color:#c11919;font-weight:bold}
.ratings-inline .rt.good   {color:#1a7f37}
.ratings-inline .rt.ok     {color:#d97706}
.ratings-inline .rt.bad    {color:#c11919}
.ratings-inline .mc.good   {color:#1a7f37}
.ratings-inline .mc.ok     {color:#d97706}
.ratings-inline .mc.bad    {color:#c11919}

/* Theaters Inline */
.theaters-inline {
  display:grid;grid-template-columns:1fr 1fr 1fr;
  gap:.4rem;font-size:.85rem;color:#555;text-transform:uppercase;
}
.theaters-inline span{
  text-align:center;font-weight:600;
}
.theaters-inline .cinema-logo {
  display:inline-block;
  padding:.2rem .3rem;
  background:#bbb;
  color:#fff;
  font-size:.85rem;
  border-radius:4px;
  min-width: 28px;  /* Ensure consistent width for the logos */
  text-align: center;
  justify-content: center;
  width: 85%;
}

/* Button styling for different categories */
.buttons-line {
  margin-top: 0.3rem;  /* Consistent with the space between ratings and cinema buttons */
  display: flex;
  flex-wrap: wrap;                  /* Allows buttons to wrap to the next line if needed */
  gap: 0.3rem;                      /* Space between buttons on the same line */
  justify-content: flex-start;      /* Align buttons to the left (you can change this if you want center alignment) */
}

.new-button {
  background-color: yellow;
  color: black;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap; /* Prevents text from wrapping */
}

.kids-button {
  background-color: darkgreen;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap; /* Prevents text from wrapping */
}

.next-showtimes-button {
  background-color: darkgrey;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap; /* Prevents text from wrapping */
}

.event-button {
  background-color: midnightblue;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap; /* Prevents text from wrapping */
}

.runtime-button {
  background-color: darkblue;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap; /* Prevents text from wrapping */
}

.release-date-button {
  background-color: darkgray;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap; /* Prevents text from wrapping */
}

.book-button {
  background-color: darkslateblue;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap; /* Prevents text from wrapping */
}

.soon-button {
  background-color: dodgerblue;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap; /* Prevents text from wrapping */
}

.content-rating-button {
  background-color: darkgreen;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap;  /* Prevent text from wrapping */
}

.web-button {
  background-color: darkred;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap;
}

/* Dark Mode Adjustments */
@media(prefers-color-scheme:dark){
  body { background:#000; color:#e0e0e0; }
  .card { background:#111; border-color:#222; }
  .ratings-inline, .theaters-inline { color:#ccc; }
  .theaters-inline .cinema-logo { background:#555; }  /* Dark mode background */
}
"""


HTML_TMPL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
  <title>🎬 Pathé Den Haag · {formatted_date}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>{css}</style>
</head><body>
  <h1>🎬 Pathé Den Haag · {formatted_date}</h1>
  <div class="grid">
    {cards}
  </div>
  <footer style="margin-top:1rem;font-size:.75rem;">
    Generated {now} · Source: Pathé API + OMDb
  </footer>
</body></html>
"""

def build_html(shows: List[dict], date: str, cinemas: Dict[str, Set[str]], zone_data: Dict[str, dict]) -> str:
    formatted_date = dt.datetime.strptime(date, "%Y-%m-%d").strftime("%B %-d")  # Correct format for month and day

    cards: List[str] = []
    for s in shows:
        # poster fallback
        md = (s.get("posterPath") or {}).get("md")
        src = md or s.get("omdbPoster") or ""
        img = f'<img src="{src}" alt="{s["title"]}">' if src else '<div class="card-no-image">No image</div>'

        # title
        title = s.get("title", "")

        # Fetch true values for isKids + bookable from zone_data
        zd        = zone_data.get(s.get("slug", ""), {})
        is_kids   = zd.get("isKids", False)
        bookable  = zd.get("bookable", False)

        # Get the duration from Pathé API and append ' min'
        duration = s.get("duration", None)  # Ensure this key exists in the show data
        runtime = f"{duration} min" if duration else ""  # Format it as 'duration min'

        # Get contentRating ref (handle both dict & list)
        content_rating_ref = ""
        cr = s.get("contentRating")
        if isinstance(cr, dict):
            content_rating_ref = cr.get("ref", "")
        elif isinstance(cr, list) and cr:
            content_rating_ref = cr[0].get("ref", "")

        # Only extract the digits and show e.g. "6+"
        import re
        m = re.search(r"(\d+)", content_rating_ref or "")
        if m:
            content_rating = f"{m.group(1)}+"
        else:
            content_rating = ""

        # Create buttons for the movie based on Pathé API data
        buttons = []

        # "Runtime" button from Pathé API
        if runtime:
            buttons.append(f'<span class="runtime-button">{runtime}</span>')

        # "Next Showtimes" button
        next_showtimes = s.get("next24ShowtimesCount", 0)
        if next_showtimes > 0:
            buttons.append(f'<span class="next-showtimes-button">{next_showtimes}/24</span>')

        # "Kids" button if movie is a kids movie
        if is_kids:
            buttons.append(f'<span class="kids-button">Kids</span>')

        # Add the content-rating button only for kids movies
        if is_kids and content_rating:
            buttons.append(f'<span class="content-rating-button">{content_rating}</span>')

        # "Event" button
        if s.get("specialEvent"):
            buttons.append(f'<span class="event-button">Event</span>')

        # Additional buttons for cases where next24ShowtimesCount is 0
        if next_showtimes == 0:
            # Button based on release date (e.g. “1 Jan”)
            release_date = s.get("releaseAt", [""])[0]
            if release_date:
                d = dt.datetime.strptime(release_date, "%Y-%m-%d")
                # Day without leading zero, and month abbrev with only first letter capitalized
                release_day_month = f"{d.day} {d.strftime('%b')}"
                buttons.append(f'<span class="release-date-button">{release_day_month}</span>')

            # Button for booking availability (only if bookable from zone_data)
            if bookable:
                buttons.append(f'<span class="book-button">Book</span>')

            # Button for coming soon (if applicable)
            if not bookable and s.get("isComingSoon"):
                buttons.append(f'<span class="soon-button">Soon</span>')

        # “Web” leak alert
        if s.get("isLeaked"):
            buttons.append('<span class="web-button">Web</span>')

        # Add the "NEW" button last in the same line if applicable
        if s.get("isNew"):
            buttons.append(f'<span class="new-button">New</span>')

        # Join all the buttons together (on a new line)
        buttons_html = ' '.join(buttons)
        
        # title with NO "NEW" button next to it
        title_html = f'<h2 class="card-title">{title}</h2>'

        # Create the button line (if any buttons exist)
        buttons_line = f'<div class="buttons-line">{buttons_html}</div>' if buttons_html else ""

        # ratings
        imdb = s.get("omdbRating")
        spans = []

        # Add tiny logos before the ratings if available
        if imdb: 
            spans.append(f'<div class="rating-block"><span class="imdb {cls_imdb(imdb)}">'
                         f'<img src="logos/imdb.svg" alt="IMDb"></span><span><strong>{imdb}</strong></span></div>')
        if s.get("rtRating"):
            spans.append(f'<div class="rating-block"><span class="rt {cls_rt(s.get("rtRating"))}">'
                         f'<img src="logos/rt.svg" alt="RT"></span><span>{s.get("rtRating")}</span></div>')
        if s.get("mcRating"):
            spans.append(f'<div class="rating-block"><span class="mc {cls_mc(s.get("mcRating"))}">'
                         f'<img src="logos/mc.svg" alt="MC"></span><span>{s.get("mcRating")}</span></div>')

        ratings_html = f'<div class="ratings-inline">{"".join(spans)}</div>'

        # theaters presence
        thr_spans = []
        slug = s.get("slug", "")
        for key, name in FAV_CINEMAS:
            mark = name if slug in cinemas.get(key, set()) else ""
            thr_spans.append(f'<span class="cinema-logo">{mark[:2].upper()}</span>' if mark else '')
        theaters_html = f'<div class="theaters-inline">{"".join(thr_spans)}</div>'

        # link
        href = s.get("imdbID") and f'https://www.imdb.com/title/{s["imdbID"]}' or "#"

        cards.append(
            f'<a class="card" href="{href}" target="_blank">'
            f'{img}'
            f'<div class="card-body">'
            f'{title_html}'  # Title with no "NEW" button next to it
            f'{ratings_html}'  # Ratings
            f'{theaters_html}'  # Cinema buttons (below the other buttons)
            f'{buttons_line}'  # Buttons (on a new line)
            f'</div></a>'
        )

    now = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    return HTML_TMPL.format(
        date=date, css=MOBILE_CSS,
        cards="\n    ".join(cards), now=now, formatted_date=formatted_date
    )


# ─────────────────────── CLI / main ───────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="Fetch Pathé Den Haag → HTML cards")
    today = dt.date.today().isoformat()
    p.add_argument("--date",  default=today, metavar="YYYY-MM-DD")
    p.add_argument("--imdb-key", help="override OMDb key")
    p.add_argument("--skip-omdb",action="store_true")
    p.add_argument("--output", default="index.html",  # Output to index.html instead of public/index.html
                   help="output HTML path")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()

def main():
    args = parse_args()
    if args.debug:
        LOG.setLevel(logging.DEBUG)

    key = args.imdb_key or OMDB_KEY_ENV
    if key and not args.skip_omdb:
        LOG.info("✅ OMDb key from %s", "CLI" if args.imdb_key else "env")
    else:
        LOG.info("ℹ️  OMDb enrichment disabled")

    LOG.info("🔗 Querying all shows for %s …", args.date)
    shows = fetch_shows(args.date)

    # zone filter
    zone_slugs = fetch_zone_slugs()
    shows = [s for s in shows if s.get("slug") in zone_slugs]
    LOG.info("· %d after zone filtering", len(shows))

    # Fetch zone data (isKids and other flags)
    zone_data = fetch_zone_data()  # This is the missing line

    # cinema presence
    cinemas: Dict[str, Set[str]] = {}
    for slug, _ in FAV_CINEMAS:
        cinemas[slug] = fetch_cinema_slugs(slug, args.date)

    # OMDb
    if key and not args.skip_omdb:
        enrich_with_omdb(shows, key)
    else:
        for s in shows:
            s["omdbRating"] = None
            s["omdbVotes"] = ""
            s["imdbID"] = None
            s["omdbPoster"] = None
            s["rtRating"] = None
            s["mcRating"] = None

    # Check for leaks 
    imdb_ids = [s["imdbID"] for s in shows if s.get("imdbID")]
    LOG.info(
        "🔍 checking leak status for %d movies … (max %d threads)",
        len(imdb_ids), LEAK_CHECK_WORKERS
    )
    leaks: Dict[str,bool] = {}
    with ThreadPoolExecutor(max_workers=LEAK_CHECK_WORKERS) as ex:
        futures = {ex.submit(check_leak, iid): iid for iid in imdb_ids}
        for fut in as_completed(futures):
            leaks[futures[fut]] = fut.result()
    # annotate each show
    for s in shows:
        s["isLeaked"] = leaks.get(s.get("imdbID"), False)

    # build & write
    html = build_html(shows, args.date, cinemas, zone_data)  # Pass zone_data here
    outd = os.path.dirname(args.output)
    if outd:
        os.makedirs(outd, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)
    LOG.info("✅ wrote %s (%d bytes)", args.output, len(html))

if __name__ == "__main__":
    main()

