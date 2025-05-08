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
    ("pathe-scheveningen", "Scheveningen"),
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

def fetch_cinema_showtimes_data(cinema_slug: str) -> dict[str, dict]:
    """
    Returns full { slug: { days: { 'YYYY-MM-DD': {...}, … }, … } } 
    for each movie in that cinema.
    """
    url = CINEMA_URL_TMPL.format(slug=cinema_slug)
    LOG.info("🔗 fetching full showtimes for cinema '%s' …", cinema_slug)
    data = get_json(url, params={"language":"nl"})
    shows_obj = data.get("shows", {}) or {}
    LOG.info("· got %d entries for %s", len(shows_obj), cinema_slug)
    return shows_obj

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
    # Prefer originalTitle if available, then strip any "(…)" suffixes
    raw_title = show.get("originalTitle") or show.get("title","")
    # remove anything in parentheses (e.g. "(OV)", "(Originele Versie)", "(NL)")
    title = re.sub(r"\s*\([^)]*\)", "", raw_title).strip()

    # Disambiguate by year
    year = show.get("productionYear")
    if not year:
        dates = show.get("releaseAt") or []
        if dates:
            year = dates[0][:4]

    params: Dict[str,str] = {
        "apikey": key,
        "t": title,       # now parenthesis‐free
        "type": "movie",
        "r": "json",
    }
    if year:
        params["y"] = str(year)

    try:
        # first try with the given year (if any)
        d = get_json(OMDB_URL, params=params.copy(), timeout=10)
        if d.get("Response") != "True":
            # only retry if we had a year and OMDb explicitly says "Movie not found!"
            error = d.get("Error", "")
            if year and error == "Movie not found!":
                # try again with one year earlier
                params_retry = params.copy()
                params_retry["y"] = str(int(year) - 1)
                LOG.debug("Retrying OMDb lookup for %r with year=%s", title, params_retry["y"])
                d = get_json(OMDB_URL, params=params_retry, timeout=10)
            # if still no good, bail out
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
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: .4rem;
  font-size: .85rem;
  color: #555;
  text-transform: uppercase;
}
.theaters-inline span{
  text-align:center;font-weight:600;
}
.theaters-inline .cinema-logo {
  display: inline-block;
  padding: .2rem .1rem;
  background: #bbb;
  color: #fff;
  font-size: .85rem;
  border-radius: 4px;
  min-width: 25px;
  text-align: center;
  justify-content: center;
  width: auto;
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
  background-color: darkorange;
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
  background-color: darkslategrey;
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

/* +++ new +++  – use when bookable */
.release-date-button.bookable {
  background-color: darkslateblue;
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
  background-color: CornflowerBlue;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap; /* Prevents text from wrapping */
}

/* Kids titles: dark-green age rating */
.kids-rating-button {      /* isKids == true */
  background-color: darkgreen;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap;
}

/* Non-kids titles: black age rating */
.adult-rating-button {          /* isKids == false */
  background-color: darkgrey;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap;
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

.next-showing-button {
  background-color: darkslateblue;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap;
}

.pre-button {
  background-color: RoyalBlue;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap;   /* no wrapping */
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

def build_html(shows: List[dict],
               date: str,
               cinemas: Dict[str, Set[str]],
               zone_data: Dict[str, dict],
               cinema_showtimes: Dict[str, dict[str, dict]]) -> str:
    formatted_date = dt.datetime.strptime(date, "%Y-%m-%d").strftime("%B %-d")  # Correct format for month and day


    cards: List[str] = []
    for s in shows:
        # title—with language normalization and optional year suffix
        raw_title = s.get("title", "")

        # 1. Normalize language suffixes (case-insensitive)
        #    Replace any "(Originele versie)" or "(OV)" → "(EN)"
        #    and "(Nederlandse versie)" → "(NL)"
        raw_title = re.sub(r"\((?:Originele Versie|OV)\)", "(EN)", raw_title, flags=re.IGNORECASE)
        raw_title = re.sub(r"\((?:Nederlandse Versie)\)", "(NL)", raw_title, flags=re.IGNORECASE)

        # 2. Append production year if releaseAt year is before this year,
        #    and title doesn't already end in '(YYYY)'
        rel_raw = s.get("releaseAt", [""])[0]
        try:
            rel_date = dt.datetime.strptime(rel_raw, "%Y-%m-%d").date()
            rel_year = rel_date.year
        except Exception:
            rel_year = None

        current_year = dt.date.today().year
        if rel_year and rel_year < current_year and not re.search(r"\(\d{4}\)$", raw_title):
            title = f"{raw_title} ({rel_year})"
        else:
            title = raw_title

        # ─── NEW POSTER + DEFAULT “no poster” IMAGE ────────────────
        md  = (s.get("posterPath") or {}).get("md")
        # fall back to OMDb poster, then to our noposter.jpg
        src = md or s.get("omdbPoster") or "logos/noposter.jpg"

        if s.get("imdbID"):
            href = f'https://www.imdb.com/title/{s["imdbID"]}'
        else:
            q = s.get("title", "")
            href = f'https://www.imdb.com/find/?q={q}'

        # always render an <img>; if it's our noposter.jpg you'll see that instead
        img = (
            f'<a href="{href}" target="_blank">'
            f'<img src="{src}" alt="{title}" border=0>'
            f'</a>'
        )


        # Fetch true values for isKids + bookable from zone_data
        zd        = zone_data.get(s.get("slug", ""), {})
        is_kids   = zd.get("isKids", False)
        bookable  = zd.get("bookable", False)

        # Get the duration from Pathé API and format as hours + minutes
        duration = s.get("duration", None)
        if duration:
            h, m = divmod(duration, 60)
            runtime = f"{h}h{m}" if h else f"{m}m"
        else:
            runtime = ""

        # Get contentRating ref (handle both dict & list)
        content_rating_ref = ""
        cr = s.get("contentRating")
        if isinstance(cr, dict):
            content_rating_ref = cr.get("ref", "")
        elif isinstance(cr, list) and cr:
            content_rating_ref = cr[0].get("ref", "")

        # ─── Final age-rating normaliser ──────────────────────────
        #  • "al"          → "AL"
        #  • "6ans"        → "6+"
        #  • "12ans"       → "12+"
        #  • plain digits  → "9+"   (etc.)
        #  • "unclassified" or empty → omit the badge
        val = (content_rating_ref or "").strip().lower().lstrip("-")
        if not val or val == "unclassified":
            content_rating = "N/A"                    # skip badge entirely
        elif val == "al":
            content_rating = "AL"
        elif val.endswith("ans") and val[:-3].isdigit():
            content_rating = f"{int(val[:-3])}+"
        elif val.isdigit():
            content_rating = f"{val}+"
        else:
            content_rating = val.upper()           # safe fallback
        # Create buttons for the movie based on Pathé API data
        buttons = []
        # Compute showtimes once, up front
        next_showtimes = s.get("next24ShowtimesCount", 0)

        # Now playing: next-showtimes + event
        if next_showtimes > 0:
            buttons.append(f'<span class="next-showtimes-button">{next_showtimes}x</span>')

        # Upcoming: either release-date (if not yet released) or next-showing (if already released)
        if next_showtimes == 0:
            rel = s.get("releaseAt", [""])[0]
            if rel:
                show_date = dt.datetime.strptime(rel, "%Y-%m-%d").date()
                query_date = dt.datetime.strptime(date, "%Y-%m-%d").date()

                if show_date <= query_date:
                    # already released → add the “next showing” badge
                    upcoming_dates: list[str] = []
                    for cin_slug in cinema_showtimes:
                        entry = cinema_showtimes[cin_slug].get(s["slug"], {})
                        upcoming_dates += list(entry.get("days", {}).keys())
                    if upcoming_dates:
                        nxt = sorted(upcoming_dates)[0]
                        dtobj = dt.datetime.strptime(nxt, "%Y-%m-%d")
                        label = f"{dtobj.day} {dtobj.strftime('%b')}"
                        buttons.append(f'<span class="next-showing-button">{label}</span>')
                else:
                    # not yet released  →  may have a pré-première (AVP)
                    future_days: list[dt.date] = []
                    for cin_slug in cinema_showtimes:
                        for d in cinema_showtimes[cin_slug].get(s["slug"], {}).get("days", {}):
                            try:
                                d0 = dt.datetime.strptime(d, "%Y-%m-%d").date()
                                if d0 > query_date:
                                    future_days.append(d0)
                            except ValueError:
                                pass
                    first_upcoming = min(future_days) if future_days else None

                    is_avp = any(t.lower() == "avp" for t in s.get("tags", []))
                    if first_upcoming and first_upcoming < show_date:
                        is_avp = True

                    # ① optional royal-blue “pre” button
                    if is_avp and first_upcoming:
                        pre_label = f"{first_upcoming.day} {first_upcoming.strftime('%b')}"
                        buttons.append(f'<span class="pre-button">{pre_label}</span>')

                    # ② always add the official release date badge
                    if show_date.year < query_date.year:
                        rel_label = str(show_date.year)
                    else:
                        rel_label = f"{show_date.day} {show_date.strftime('%b')}"
                    rel_cls = "release-date-button"
                    if bookable:
                        rel_cls += " bookable"
                    buttons.append(f'<span class="{rel_cls}">{rel_label}</span>')

        # Upcoming: bookable only if release date is in the future, otherwise Soon
        if next_showtimes == 0:
            # parse “today” from the outer date string
            today = dt.datetime.strptime(date, "%Y-%m-%d").date()
            # grab the official releaseAt
            release_raw = s.get("releaseAt", [""])[0]
            if release_raw:
                rel_date = dt.datetime.strptime(release_raw, "%Y-%m-%d").date()
            else:
                rel_date = today
            # only show “Book” when bookable AND release is strictly after today
            if bookable and rel_date > today:
                buttons.append(f'<span class="book-button">Out</span>')
            # otherwise, if coming soon, show Soon
            elif s.get("isComingSoon"):
                buttons.append(f'<span class="soon-button">Soon</span>')

        # Runtime button
        if runtime:
            buttons.append(f'<span class="runtime-button">{runtime}</span>')

        # Unified age-rating button
        if content_rating:                     # show it for every title
            rating_class = "kids-rating-button" if is_kids else "adult-rating-button"
            buttons.append(f'<span class="{rating_class}">{content_rating}</span>')

        # Show the flag if it is marked as a specialEvent (e.g. Classics, 50PLus, Docs, etc)
        if s.get("specialEvent"):
            flag = s.get("flag")
            if flag:
                buttons.append(f'<span class="event-button">{flag}</span>')

        # Leak alert
        if s.get("isLeaked"):
            buttons.append('<span class="web-button">Web</span>')

        # New button always last
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

        # theaters presence (all buttons now link to the film page)
        slug = s.get("slug", "")
        film_url = f"https://www.pathe.nl/nl/films/{slug}"
        thr_items = []
        for key, name in FAV_CINEMAS:
            if slug in cinemas.get(key, set()):
                code = name[:2].upper()
                thr_items.append(
                    f'<a href="{film_url}" target="_blank" style="text-decoration:none;">'
                    f'<span class="cinema-logo">{code}</span>'
                    f'</a>'
                )
        theaters_html = f'<div class="theaters-inline">{"".join(thr_items)}</div>'

        cards.append(
            f'<div class="card">'
            f'{img}'
            f'<div class="card-body">'
            f'{title_html}'
            f'{ratings_html}'
            f'{theaters_html}'
            f'{buttons_line}'
            f'</div>'
            f'</div>'
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

    # Fetch full zone shows (with isNew, isComingSoon, specialEvent, etc.)
    zone_shows = fetch_zone_shows()
    for s in shows:
        slug = s.get("slug")
        if slug in zone_shows:
            # brings in s["isNew"], s["isComingSoon"], s["specialEvent"], etc.
            s.update(zone_shows[slug])

    # Fetch zone data (isKids and other flags)
    zone_data = fetch_zone_data()

    # cinema presence + full showtimes map
    cinemas: Dict[str, Set[str]] = {}
    cinema_showtimes: Dict[str, dict[str, dict]] = {}
    for slug, _ in FAV_CINEMAS:
        # grab exactly those movies playing on args.date
        cinemas[slug] = fetch_cinema_slugs(slug, args.date)
        # still fetch the full multi-day schedule for your “next-showing” button logic
        cinema_showtimes[slug] = fetch_cinema_showtimes_data(slug)

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

    # reorder shows
    query_date = dt.datetime.strptime(args.date, "%Y-%m-%d").date()

    def sort_key(s: dict):
        cnt = s.get("next24ShowtimesCount", 0)
        # 0️⃣ Now playing → highest count first
        if cnt > 0:
            return (0, -cnt, dt.date.min)

        # parse the “official” releaseAt date
        raw = s.get("releaseAt", [""])[0]
        try:
            release_date = dt.datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            release_date = None

        # gather any future show dates across all cinemas
        upcoming: list[dt.date] = []
        for showtimes in cinema_showtimes.values():
            days = showtimes.get(s["slug"], {}).get("days", {})
            for d in days:
                try:
                    d0 = dt.datetime.strptime(d, "%Y-%m-%d").date()
                    if d0 > query_date:
                        upcoming.append(d0)
                except ValueError:
                    pass

        # build the list of candidates:
        #  • all upcoming show dates
        #  • plus release_date if it's strictly in the future
        candidates = upcoming.copy()
        if release_date and release_date > query_date:
            candidates.append(release_date)

        # fixed date sorting
        if candidates:
            sort_date = min(candidates)                # first show after today
        elif release_date:                             #   ← NEW fallback
            sort_date = release_date                   # use it even if it’s in the past
        else:
            sort_date = dt.date.max                    # nothing at all – push to the end

        # 1️⃣ Upcoming / future-release group, sorted by that date
        return (1, sort_date, dt.date.min)

    shows.sort(key=sort_key)


    # build & write
    html = build_html(shows, args.date, cinemas, zone_data, cinema_showtimes)
    outd = os.path.dirname(args.output)
    if outd:
        os.makedirs(outd, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)
    LOG.info("✅ wrote %s (%d bytes)", args.output, len(html))

if __name__ == "__main__":
    main()

