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
import pathlib
import logging
import sqlite3
import os
import sys
import time
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Set
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import json
from html import escape

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

NEW_BOOKABLE_HOURS = 72    # “0day/…day” badge fades over 7 days
NEW_SOON_HOURS     =  24    # “New” badge stays for 3 days

DB_PATH   = os.path.join(os.path.dirname(__file__), "movies.db")

FAV_CINEMAS = [
    ("pathe-ypenburg",   "Ypenburg"),
    ("pathe-spuimarkt",  "Spui"),
    ("pathe-buitenhof",  "Buitenhof"),
    ("pathe-scheveningen", "Scheveningen"),
]

# Remove any (...) when a movie has no imdbID for the fallback link
PAREN_RE = re.compile(r"\s*\([^)]*\)")

os.environ["TZ"] = "Europe/Amsterdam"
time.tzset()

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

def fetch_cinema_showtimes_data(cinema_slug: str, for_date: str) -> dict[str, dict]:
    """
    Return a mapping that *does* contain showtimes:

        { "<film-slug>": {
              "days": {
                  "YYYY-MM-DD": {
                      "showtimes": ["HH:MM", "HH:MM", …],
                      ...           # other keys from the original payload
                  },
                  ...
              },
              ...                   # untouched keys (bookable, tags, etc.)
        }}

    It first fetches the aggregate /cinema/…/shows payload and then,
    **only for the film/date combinations we care about**, calls the
    per-film showtimes endpoint once to inject the real times.
    """
    url = CINEMA_URL_TMPL.format(slug=cinema_slug)
    LOG.info("🔗 fetching full showtimes for cinema '%s' …", cinema_slug)
    root = get_json(url, params={"language": "nl"}) or {}
    shows_obj = root.get("shows", {}) or {}

    # figure out which date(s) matter for this run
    today = for_date
    for film_slug, film_data in shows_obj.items():
        for day, day_info in film_data.get("days",{}).items():
            if day != today:
                continue

            # call the per-film endpoint
            st_url = (
                f"https://www.pathe.nl/api/show/{film_slug}"
                f"/showtimes/{cinema_slug}/{day}"
            )
            try:
                arr = get_json(st_url, params={"language": "nl"}, timeout=10)
                times = [itm["time"][-8:-3] for itm in arr if "time" in itm]
                day_info["showtimes"] = times        # inject for later use
            except Exception as exc:
                LOG.debug("showtimes lookup failed %s – %s", st_url, exc)

    LOG.info("· got %d entries (with showtimes) for %s", len(shows_obj), cinema_slug)
    return shows_obj


def fetch_zone_shows() -> dict:
    """ Fetch shows from the Den Haag zone to retrieve kids data and other info """
    LOG.info("🔗 Fetching Den Haag zone shows …")
    data = get_json(ZONE_URL, params={"language":"nl"})
    shows = {s["slug"]: s for s in data.get("shows", []) if "slug" in s}
    LOG.info("· Got %d zone shows", len(shows))
    return shows

def fetch_zone_data() -> Dict[str, dict]:
    """Collect every zone-specific attribute we need in one place."""
    LOG.info("🔗 Fetching zone data for Den Haag …")
    data = get_json(ZONE_URL, params={"language": "nl"})
    zone_data: Dict[str, dict] = {}
    for show in data.get("shows", []):
        tags = show.get("tags") or []
        zone_data[show["slug"]] = {
            "isKids":      show.get("isKids", False),
            "bookable":    show.get("bookable", False),

            # keep the **zone** figure for the 24-hour badge
            "zoneNext24":  show.get("next24ShowtimesCount", 0),

            # premium formats available in the zone
            "hasIMAX":     "imax"  in tags,
            "hasDolby":    "dolby" in tags,
            "hasAVP":      "avp"      in tags,
            "isLast":    "lastchance" in tags,
        }
    return zone_data

# ──────────────────── zone-only enrichment helper ────────────────────
def _merge_single_record(orig: dict) -> dict:
    """
    Enrich a zone-only stub with the full JSON from
    /api/show/{slug}?language=nl  and normalise `releaseAt`
    so the rest of the pipeline keeps working unchanged.
    """
    url  = f"https://www.pathe.nl/api/show/{orig['slug']}?language=nl"
    full = get_json(url, params={})

    # normalise  releaseAt  ───────────────────────────────
    # • normal shows API  → ["YYYY-MM-DD"]
    # • single-show API   → {"NL_NL":"YYYY-MM-DD"}
    rel = full.get("releaseAt")
    if isinstance(rel, dict):
        rel = [rel.get("NL_NL", "")]
    elif rel is None:
        rel = [""]
    full["releaseAt"] = rel

    # copy only the fields we actually need later on
    wanted = (
        "releaseAt", "duration", "posterPath",
        "originalTitle", "contentRating", "tags"
    )
    for k in wanted:
        orig[k] = full.get(k, orig.get(k))

    return orig


# ──────────────────── recent-film DB helpers ────────────────────
def _init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    # first-sighting timestamps of a new movie card
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            slug       TEXT PRIMARY KEY,
            first_seen TEXT                -- ISO timestamp (UTC)
        )
    """)

    # first-sighting of “bookable == True” for n-day button
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen_bookable (
            slug       TEXT PRIMARY KEY,
            first_seen TEXT       -- ISO timestamp in UTC
        )
    """)

    # first-sighting of “isComingSoon == True” for New button
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen_soon (
            slug       TEXT PRIMARY KEY,
            first_seen TEXT                -- ISO timestamp (UTC)
        )
    """)

    # one OMDb snapshot per slug per calendar-day
    cur.execute("""
        CREATE TABLE IF NOT EXISTS omdb_cache (
            slug        TEXT,
            yyyymmdd    TEXT,              -- e.g. 2025-05-13
            omdbRating  TEXT,
            omdbVotes   TEXT,
            imdbID      TEXT,
            omdbPoster  TEXT,
            rtRating    TEXT,
            mcRating    TEXT,
            runtime     TEXT,
            PRIMARY KEY(slug,yyyymmdd)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS metadata (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_updated', ?)",
        [dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds") + "Z"]
    )

    conn.commit()
    return conn

_DB_CONN: Optional[sqlite3.Connection] = None
def _db() -> sqlite3.Connection:
    global _DB_CONN
    if _DB_CONN is None:
        _DB_CONN = _init_db()
    return _DB_CONN

def register_and_age(slug: str) -> tuple[float, bool]:
    """
    Ensure `slug` is in the DB and return:
      (hours_ago_float, is_recent_bool)

    • The timestamp is always stored in explicit UTC.
    • Legacy rows that were saved as *naive* UTC strings are upgraded
      on-the-fly so arithmetic never fails.
    """
    now = dt.datetime.now(dt.timezone.utc)                      # aware

    cur = _db().cursor()
    cur.execute("SELECT first_seen FROM seen WHERE slug=?", (slug,))
    row = cur.fetchone()

    if row is None:                                             # brand-new movie
        cur.execute(
            "INSERT INTO seen VALUES(?,?)",
            (slug, now.isoformat(timespec="seconds"))
        )
        _db().commit()
        return 0.0, True                                        # 0 h ago → recent

    first_seen = dt.datetime.fromisoformat(row[0])

    # upgrade old, naive UTC strings (from earlier runs)
    if first_seen.tzinfo is None:
        first_seen = first_seen.replace(tzinfo=dt.timezone.utc)

    hours_ago = (now - first_seen).total_seconds() / 3600.0
    return hours_ago, hours_ago < NEW_SOON_HOURS

def register_and_age_bookable(slug: str, is_bookable: bool) -> tuple[float, bool]:
    """
    Track when a film first becomes bookable (bookable==True).
    Returns (hours_since_first_bookable, is_first_<NEW_HOURS>_bookable).
    """
    now = dt.datetime.now(dt.timezone.utc)
    cur = _db().cursor()

    # only care when it turns bookable
    if not is_bookable:
        return float('inf'), False

    cur.execute("SELECT first_seen FROM seen_bookable WHERE slug=?", (slug,))
    row = cur.fetchone()

    if row is None:
        cur.execute(
            "INSERT INTO seen_bookable VALUES(?,?)",
            (slug, now.isoformat(timespec="seconds"))
        )
        _db().commit()
        return 0.0, True

    first = dt.datetime.fromisoformat(row[0])
    if first.tzinfo is None:
        first = first.replace(tzinfo=dt.timezone.utc)

    hours_ago = (now - first).total_seconds() / 3600.0
    return hours_ago, hours_ago < NEW_BOOKABLE_HOURS

def register_and_age_soon(slug: str, is_coming_soon: bool) -> tuple[float, bool]:
    """
    Track when a film is first seen with isComingSoon == True.
    Returns (hours_since_first_soon, is_within_NEW_HOURS_window).
    """
    if not is_coming_soon:                       # nothing to record / show
        return float('inf'), False

    now = dt.datetime.now(dt.timezone.utc)
    cur = _db().cursor()

    cur.execute("SELECT first_seen FROM seen_soon WHERE slug=?", (slug,))
    row = cur.fetchone()

    if row is None:                              # brand-new “Soon”
        cur.execute("INSERT INTO seen_soon VALUES(?,?)",
                    (slug, now.isoformat(timespec='seconds')))
        _db().commit()
        return 0.0, True

    first = dt.datetime.fromisoformat(row[0])
    if first.tzinfo is None:
        first = first.replace(tzinfo=dt.timezone.utc)

    hrs = (now - first).total_seconds() / 3600.0
    return hrs, hrs < NEW_SOON_HOURS


# ──────────────────── OMDb enrichment ───────────────────
def fetch_omdb_data(show: dict, key: str) -> dict:
    # Prefer originalTitle if available, then strip any "(…)" suffixes
    raw_title = show.get("originalTitle") or show.get("title","")
    # remove anything in parentheses (e.g. "(OV)", "(Originele Versie)", "(NL)")
    title = re.sub(r"\s*\([^)]*\)", "", raw_title).strip()

    # ─── Disambiguate by year (robust against all API shapes) ─────
    year = show.get("productionYear")              # ① explicit field

    if not year:
        rel = show.get("releaseAt")                # may be list | str | dict | None

        if isinstance(rel, list) and rel:          # ② list → first element
            year = rel[0][:4]

        elif isinstance(rel, str) and rel:         # ③ plain string "YYYY-MM-DD"
            year = rel[:4]

        elif isinstance(rel, dict):                # ④ dict e.g. { "start": ... }
            for fld in ("start", "releaseAt", "date"):   # ‼️  avoid shadowing the function arg
                val = rel.get(fld)
                if val:
                    year = str(val)[:4]
                    break

    if year:
        year = str(year)             # normalise for use in params


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


def enrich_with_omdb(shows: List[dict], api_key: Optional[str]) -> None:
    """
    • Always restores today's data from omdb_cache.
    • Calls the OMDb API only for (slug,today) rows missing in the cache *and*
      only when an API key is supplied.
    • Fresh answers are written back into the same cache table.
    """
    today = dt.date.today().isoformat()
    cur   = _db().cursor()

    # ── 1. hydrate from cache ───────────────────────────────────────────
    missing: list[dict] = []
    for s in shows:
        slug = s["slug"]
        row  = cur.execute(
            "SELECT omdbRating,omdbVotes,imdbID,omdbPoster,"
            "       rtRating,mcRating,runtime "
            "FROM omdb_cache WHERE slug=? AND yyyymmdd=?",
            (slug, today)
        ).fetchone()

        if row:
            keys = ["omdbRating","omdbVotes","imdbID",
                    "omdbPoster","rtRating","mcRating","runtime"]
            s.update(dict(zip(keys, row)))
        else:
            missing.append(s)

    # nothing missing → done
    if not missing:
        LOG.info("🔍 OMDb cache hit for all %d titles – no API calls", len(shows))
        return

    # no API key → can't query, but still ensure all keys exist
    if not api_key:
        LOG.info("🔍 OMDb key absent – skipped %d live look-ups", len(missing))
        for s in missing:
            for k in ("omdbRating","omdbVotes","imdbID",
                      "omdbPoster","rtRating","mcRating","runtime"):
                s.setdefault(k, None)
        return

    # ── 2. live look-ups for the *missing* slugs ───────────────────────
    LOG.info("🔍 OMDb cache miss for %d titles – querying API …", len(missing))
    with cf.ThreadPoolExecutor(max_workers=MAX_OMDB_WORKERS) as px:
        futs = {px.submit(fetch_omdb_data, s, api_key): s for s in missing}
        for fut in cf.as_completed(futs):
            s    = futs[fut]
            data = fut.result() or {}

            # copy into the card
            for k in ("imdbRating","imdbVotes","imdbID",
                      "omdbPoster","rtRating","mcRating","runtime"):
                target = "omdbRating" if k == "imdbRating" else \
                         "omdbVotes"  if k == "imdbVotes"  else k
                s[target] = data.get(k)

            # persist in cache  (use INSERT OR REPLACE for idempotency)
            cur.execute("""
                INSERT OR REPLACE INTO omdb_cache
                (slug,yyyymmdd,omdbRating,omdbVotes,imdbID,
                 omdbPoster,rtRating,mcRating,runtime)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                s["slug"], today,
                s.get("omdbRating"), s.get("omdbVotes"), s.get("imdbID"),
                s.get("omdbPoster"), s.get("rtRating"), s.get("mcRating"),
                s.get("runtime")
            ))
    _db().commit()



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
html{ -webkit-text-size-adjust:100%; }
body{
  margin:1rem;
  font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  /* default (light) colours */
  background:#f6f6f7;
  color:#111;

  /* 🆕 let iOS/Safari know that dark-mode variants are present */
  color-scheme: light dark;
}
h1{font-size:1.5rem;margin:0 0 1rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));grid-gap:0.5rem}
/* ───── Card visuals ──────────────────────────────────────────── */
.card{
  display:block;
  border:1px solid #ddd;
  border-radius:8px;
  overflow:hidden;
  text-decoration:none;
  color:inherit;
  background:#fff;
  touch-action: pan-y;
  /* base shadow (visible everywhere) */
  box-shadow:0 1px 4px #0003;
  transition:transform .15s,box-shadow .15s;
  position: relative;          /* needed for absolute dots */
}

/* ①  Desktop / trackpad — lift on hover  */
@media (hover:hover) and (pointer:fine){
  .card:hover{
    transform:translateY(-4px);
    box-shadow:0 4px 12px #0004;
  }
}

/* ②  Touch screens — slight sink on active press  */
@media (hover: none) and (pointer: coarse) {
  .card:active {
    transform: scale(.99);
    box-shadow: 0 1px 2px #0002;
  }
}

.card img{width:100%;display:block;object-fit:cover}
.card-no-image{width:100%;padding-top:150%;background:#eee;display:flex;align-items:center;justify-content:center;color:#666;font-size:.8rem}
.card-body{padding:.5rem}
.card-title{font-size:1rem;line-height:1.2;margin:0}

/* hide poster image & title on any alt-face 
/* .card.alt-face > a > img, */
/* .card.alt-face .card-title { */
.card.alt-face > a > img {
  display: none;
}

/* title spacing ONLY while a non-primary face is visible */
.card.alt-face .card-title{
  margin-top: .55rem;
}

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

.ratings-inline img{
  height:14px;        /* ≈0.9 rem on desktop – tweak if you like */
  width:auto;
  margin-bottom:.2rem;
  vertical-align:middle;
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

/* Cinema badges (YP / BU / SC) – identical look everywhere */

.theaters-inline .cinema-logo,
.faces .cinema-logo {
  display:inline-block;
  padding:.2rem .1rem;
  background:darkgrey;
  color:#fff;
  font-weight:bold;
  font-size: .85rem;
  border-radius:4px;
  text-align:center;
  white-space:nowrap;
  min-width:25px;     
}

 .faces .cinema-logo {
   margin-top: .3rem;
 }
 /* any cinema‐logo in face 1,2,3… */
.faces .face:not([data-face="0"]) .cinema-logo {
  background: #555;
  color: #fff;
}

/* any showtime‐button in face 1,2,3… */
.faces .face:not([data-face="0"]) .next-showtimes-button{
  background: darkgrey;
  color: #fff;
}

/* Quick-filter toolbar */
.filter-bar{
  position:sticky;   /* ← makes it “float” */
  top:0;             /* ← stick to the very top of the viewport */
  z-index:20;        /* ← stay above the cards */
  display:flex;
  gap:.5rem;
  overflow-x:auto;
  padding:.5rem 0 .75rem;
  background:#f6f6f7;          /* light-mode background */
}

/* keep the dark-mode backdrop in sync */
@media(prefers-color-scheme:dark){
  .filter-bar{ background:#000; }
}

.chip{
  flex:0 0 auto;
  font-size:.8rem;
  padding:.25rem .6rem;
  border-radius:16px;
  background:#ddd;
  color:#000;
  border:1px solid #ccc;
  cursor:pointer;
  user-select:none;
}
.chip.active{
  background:#333;
  color:#fff;
}

/* Button styling for different categories */
.buttons-line {
  margin-top: 0.3rem;  /* Consistent with the space between ratings and cinema buttons */
  display: flex;
  flex-wrap: wrap;                  /* Allows buttons to wrap to the next line if needed */
  gap: 0.3rem;                      /* Space between buttons on the same line */
  justify-content: flex-start;      /* Align buttons to the left (you can change this if you want center alignment) */
}

/* New-Bookable badge – fades as time passes (see --alpha) */
.new-bookable-button{
  display:inline-block;
  padding:2px 6px;
  font-weight:bold;
  font-size:.8rem;
  border-radius:4px;
  background:#ff9500;     /* vivid orange when brand-new */
  color:#fff;
  opacity:var(--alpha);   /* 1 → fully opaque, 0.3 → barely there */
  transition:opacity .15s;
}

/* New-Soon badge – fixed OrangeRed, no fading */
.new-soon-button{
  display:inline-block;
  padding:2px 6px;
  font-weight:bold;
  font-size:.8rem;
  border-radius:4px;
  background:#FF4500;     /* OrangeRed */
  color:#fff;
}

.next-showtimes-button {
  background-color: lightgrey;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap; /* Prevents text from wrapping */
}

.event-button {
  background-color: darkblue;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap; /* Prevents text from wrapping */
}

.runtime-button {
  background-color: darkgrey; /* was lightslategrey */
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap; /* Prevents text from wrapping */
}

.release-date-button {
  background-color: lightgrey;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap; /* Prevents text from wrapping */
}

/* +++ new +++  – use when bookable */
.release-date-button.bookable {
  background-color: CornflowerBlue;
}

.book-button {
  background-color: CornflowerBlue;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap; /* Prevents text from wrapping */
}

.soon-button {
  background-color: lightgrey;
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
  background-color: lightgrey;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap;
}

.web-button {
  background-color: black;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap;
}

.next-showing-button {
  background-color: CornflowerBlue;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap;
}

.pre-button {
  background-color: LightSkyBlue;
  color: white;
  padding: 2px 5px;
  font-weight: bold;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap;   /* no wrapping */
}


/* Dark Mode Adjustments */
@media(prefers-color-scheme:dark){
  html,body{ background:#000; color:#e0e0e0; }
  .card { background:#111; border-color:#222; }
  .ratings-inline, .theaters-inline { color:#ccc; }
  .theaters-inline .cinema-logo { background:#555; }  /* Dark mode background */
  .faces .face:not([data-face="0"]) .cinema-logo { background: #555; color: #fff; }

}

/* utility for JS filter */
.hidden{display:none!important;}


/* dim watched cards when the “Watched” chip is active */
.card.dim { opacity: .5; }

/* snackbar that slides up from the bottom */
#snackbar{
  position:fixed;left:50%;bottom:-60px;transform:translateX(-50%);
  background:#333;color:#fff;padding:.6rem 1rem;border-radius:6px;
  font-size:.9rem;display:flex;gap:.8rem;align-items:center;
  transition:bottom .25s;
}
#snackbar.visible{ bottom:1.2rem; }

/* Snackbar action button — make it match the surrounding text */
#snackbar button{
  background:none;
  border:none;
  color:#4ea3ff;
  font-weight:600;

  /* NEW */
  font-size:inherit;        /* or simply: font: inherit; */
  line-height:inherit;      /* keeps vertical rhythm identical */

  /* optional niceties */
  padding:0;
  cursor:pointer;
  -webkit-appearance:none;  /* nuke iOS default bevel */
}


/* Prevent iOS long-press text-selection / callout on cards */
.card, .card *{
  -webkit-user-select: none;   /* Safari / old WebKit */
  user-select: none;
  -webkit-touch-callout: none; /* iOS context menu */
}

/* ─── tiny iOS-style page dots in a pill ────────────────────────── */
.pager-dots{
  position:absolute;
  top:.35rem;
  right:.4rem;

  /* >>> the new “pill” <<< */
  padding:.2rem .45rem;              /* space around the dots      */
  border-radius:9999px;              /* super-rounded ➜ capsule    */
  background:#0007;                  /* translucent black in light */
  backdrop-filter:saturate(150%) blur(3px);   /* subtle glassy feel */
  
  display:flex;
  gap:.3rem;
  align-items:center;
}

.pager-dots span{
  width:.33rem; height:.33rem;
  border-radius:50%;
  background:#ccc;                   /* inactive */
  flex:0 0 auto;
}
.pager-dots span.active{
  background:#fff;                   /* active   */
}

/* dark-mode adjustments */
@media(prefers-color-scheme:dark){
  .pager-dots{ background:#fff3; }    /* translucent white pill    */
  .pager-dots span{ background:#666; }
  .pager-dots span.active{ background:#000; }
}

/* cinema faces – only one visible at a time */
.faces        { position: relative; }
.face         { display: none; }
.face.active  { display: block; }
.face{ padding-bottom:.25rem; }   /* keeps the old spacing inside a face */

.timebox  { display:flex; flex-wrap:wrap; gap:.3rem; justify-content:center; }
.showtime { padding:1px 4px; border:1px solid #ccc; border-radius:3px;
            font-variant-numeric: tabular-nums; }

.ratings-inline:empty {
  margin-top: 0;
  margin-bottom: 0;
}

/* ─── face 1: stack each cinema name above its showtimes ───────────────── */
.faces .face[data-face="1"] .cinema-block {
  display: flex;
  flex-direction: column;
  gap: .05rem;             /* space between the name and its row of buttons */
  margin-bottom: .1rem;  /* space between each cinema block */
}

/* breathing room above each cinema name */
.faces .face[data-face="1"] .cinema-block .cinema-logo {
  margin-top: 0.8rem;
}

/* only on face 1: 3 equal columns, tight gaps */
.faces .face[data-face="1"] .buttons-line {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: .2rem;
  /* no need for justify-items:center—buttons will each fill their column */
}

/* only on face 1: make every pill fill its grid‐cell uniformly */
.faces .face[data-face="1"] .next-showtimes-button {
  width: 100%;
  box-sizing: border-box;       /* include padding in that width */
  padding: 1px 3px;             /* your tight padding */
  text-align: center;           /* center the hh:mm text */
  font-size: .8rem;
  font-variant-numeric: tabular-nums; /* ensure digits line up */
}

/* ─── bottom filter-bar on touch screens ───────────────────────────── */
@media (hover:none) and (pointer:coarse){
  .filter-bar{
    position:fixed;                /* always visible */
    bottom:0; left:0; right:0;
    top:auto;                      /* undo the old sticky-top */
    z-index:35;                    /* above cards & snackbar */

    /* iOS safe-area & a bit of breathing room */
    padding:.5rem env(safe-area-inset-left)
            calc(.75rem + env(safe-area-inset-bottom))
            env(safe-area-inset-right);

    /* raise the visual hierarchy & fight translucency */
    background:rgba(246,246,247,.95);     /* same colour, slight opacity */
    backdrop-filter:blur(10px);           /* iOS  ≥15 “frosted glass”   */
    box-shadow:0 -1px 4px #0003;
  }

  /* give the page enough room so the bar doesn’t cover content */
  body{ padding-bottom:calc(3.5rem + env(safe-area-inset-bottom)); }
}

/* dark-mode variant (same @media) */
@media (hover:none) and (pointer:coarse) and (prefers-color-scheme:dark){
  .filter-bar{ background:rgba(0,0,0,.75); }
}


"""


HTML_TMPL = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8">
  <title>Pathé Den Haag · {formatted_date}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light dark">   <!-- enables iOS dark-mode -->


 <link rel="apple-touch-icon" sizes="180x180" href="logos/apple-touch-icon.png">
 <link rel="icon" type="image/png" sizes="32x32" href="logos/favicon-32x32.png">
 <link rel="icon" type="image/png" sizes="16x16" href="logos/favicon-16x16.png">
 <link rel="manifest" href="manifest.json">

   <!-- Tell iOS Safari this is a web app -->
  <meta name="apple-mobile-web-app-capable" content="yes">
  <!-- Status bar style: default / black / black-translucent -->
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <!-- App title shown on Home Screen (if different from <title>) -->
  <meta name="apple-mobile-web-app-title" content="Pathé">

  <!-- Google Font -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap" rel="stylesheet">

  <!-- Tiny Swiper-->
  <script defer src="tiny-swipe.min.js"></script>

  <style>{css}</style>
</head>
<body>

    <h1>
      <img
        src="logos/apple-touch-icon.png"
        alt="Pathé"
        style="width:1em;height:auto;vertical-align:middle;margin-right:.25em;"
        id="refresh"
      >
      Pathé Den Haag · {formatted_date}
    </h1>

  <!-- quick-filter chips -->
  <div class="filter-bar">
    <span class="chip" data-tag="all">All</span> 
    <span class="chip" data-tag="now">Now</span>
    <span class="chip" data-tag="book">Book</span>
    <span class="chip" data-tag="new">New</span>
    <span class="chip" data-tag="soon">Soon</span>
    <span class="chip" data-tag="kids">Kids</span>
    <span class="chip" data-tag="dolby">Dolby</span>
    <span class="chip" data-tag="imdb7">IMDB 7+</span>
    <span class="chip" data-tag="future">Future</span>
    <span class="chip" data-tag="web">Web</span>
    <span class="chip" data-tag="watched">Hidden</span>
  </div>
  <div class="grid">
    {cards}
  </div>
  <footer style="margin-top:1rem;font-size:.75rem;">
    Generated {now} · Source: Pathé API + OMDb
  </footer>

  <!-- tiny JS for the quick-filter (all braces are doubled for str.format) -->
<script>
const showTimes = {show_times};      /* ← injected by .format() */

function fillShowTimes() {{
  document.querySelectorAll('.card').forEach(card => {{
    const slug      = card.dataset.slug;
    const timesByCx = showTimes[slug] || {{}};

    card.querySelectorAll('.face[data-face]:not([data-face="0"])')
        .forEach(face => {{
          // loop *each* cinema‐block inside this single face
          face.querySelectorAll('.cinema-block').forEach(block => {{
            const logoEl = block.querySelector('.cinema-logo');
            const code   = logoEl.dataset.code || logoEl.textContent.trim();
            const box    = block.querySelector('.buttons-line');

            if (timesByCx[code]?.length) {{
              box.innerHTML = timesByCx[code]
                .map(t => `<span class="next-showtimes-button">${{t}}</span>`).join('');
            }} else {{
              box.textContent = '–';
            }}
          }});
        }});
  }});
}}

function wireFaceTabs() {{
  document.querySelectorAll('.card').forEach(card => {{
    const faces = [...card.querySelectorAll('.face')];
    const pagerDots = [...card.querySelectorAll('.pager-dots span')];

    card.querySelectorAll('.theaters-inline .cinema-logo')
        .forEach((logo, idx) => {{
          logo.addEventListener('click', () => {{
            faces.forEach(f => f.classList.remove('active'));
            pagerDots.forEach(d => d.classList.remove('active'));
            faces[idx + 1].classList.add('active');   // +1 skips the primary face
            pagerDots[idx + 1].classList.add('active');

            if (idx+1 > 0) card.classList.add('alt-face');
                else card.classList.remove('alt-face');

          }});
        }});

    /* optional: poster click → return to primary face */
    card.querySelector('img')?.addEventListener('click', () => {{
      faces.forEach(f => f.classList.remove('active'));
      faces[0].classList.add('active');
    }});
  }});
}}

document.addEventListener('DOMContentLoaded', () => {{
  /* ─────────────────── DOM references ─────────────────── */
  const chips    = [...document.querySelectorAll('.chip')];
  const cards    = [...document.querySelectorAll('.card')];
  const snackbar = document.getElementById('snackbar');

  /* pull-to-refresh replacement — manual button */
  document.getElementById('refresh')
    ?.addEventListener('click', () => location.reload());

  /* ─────────────────── watched <Set> in localStorage ─────────────────── */
  const KEY = 'watchedSlugs-v1';

  const loadSet = () => new Set(JSON.parse(localStorage.getItem(KEY) || '[]'));
  const saveSet = set => localStorage.setItem(KEY, JSON.stringify([...set]));

  const watched = loadSet();

  /* ─────────────────── apply() → filter + watched visuals ─────────────────── */
    function apply() {{
      const active        = chips.filter(c => c.classList.contains('active'))
                                 .map(c => c.dataset.tag);

      const allOn         = active.includes('all');
      const watchedChipOn = active.includes('watched');

      /* ⇢ NEW: at least one thematic chip (kids, web, dolby, …) is active */
      const topicalOn     = active.some(t => !['all', 'watched'].includes(t));

      cards.forEach(card => {{
        const slug      = card.dataset.slug;
        const tags      = card.dataset.tags.split(' ');
        const isWatched = watched.has(slug);

        let show;

        if (allOn) {{
          /* “All” chip ON → always show everything */
          show = true;

        }} else if (active.length === 0) {{
          /* ★★ default view ★★ – hide future & watched cards */
          show = !tags.includes('future') && !isWatched;

        }} else {{
          /* any other combination of chips */
          show = active.some(t => tags.includes(t));

          /* hide watched **only** when neither Watched nor a topical chip is on */
          if (isWatched && !watchedChipOn && !topicalOn) {{
            show = false;
          }}

          /* Watched chip ON → force the watched card to show */
          if (watchedChipOn && isWatched) {{
            show = true;
          }}
        }}

        card.classList.toggle('hidden', !show);

        /* dim watched cards whenever they’re visible via Watched, All or topical chips */
        const dimIt = isWatched && (watchedChipOn || allOn || topicalOn);
        card.classList.toggle('dim', dimIt);
      }});
    }}

  /* ─────────────────── toggleWatch(card) ─────────────────── */
  function toggleWatch(card) {{
    const slug        = card.dataset.slug;
    const wasWatched  = watched.has(slug);

    if (wasWatched) watched.delete(slug);
    else            watched.add(slug);

    saveSet(watched);
    if (navigator.vibrate) navigator.vibrate(10);   /* tiny haptic ping */
    apply();
    showUndo(slug, wasWatched);
  }}

  /* ─────────────────── snackbar with Undo ─────────────────── */
  function showUndo(slug, wasWatched) {{
    const msg = wasWatched ? 'Restored' : 'Hidden';
    snackbar.innerHTML = `${{msg}} <button id="undo">Undo</button>`;
    snackbar.classList.add('visible');

    document.getElementById('undo').onclick = () => {{
      if (wasWatched) watched.add(slug);
      else            watched.delete(slug);
      saveSet(watched);
      apply();
      snackbar.classList.remove('visible');
    }};

    clearTimeout(snackbar._timer);
    snackbar._timer = setTimeout(() => {{
      snackbar.classList.remove('visible');
    }}, 5000);
  }}

  /* ─────────────────── chip click behaviour ─────────────────── */
    chips.forEach(chip => {{
      chip.addEventListener('click', () => {{
        chip.classList.toggle('active');

        const tag = chip.dataset.tag;
        if (tag === 'all' && chip.classList.contains('active')) {{
          /* “All” turned on → clear the rest */
          chips.forEach(c => {{ if (c !== chip) c.classList.remove('active'); }});
        }} else if (tag !== 'all') {{
          /* any other chip clicked → switch “All” off */
          chips.find(c => c.dataset.tag === 'all')?.classList.remove('active');
        }}

        apply();
        window.scrollTo({{ top: 0, behavior: 'smooth' }});
      }});
    }});

  /* ─────────────────── long-press detection (robust) ─────────────────── */
  const LONG = 1000;                     /* ms – press length for “watched”  */

  cards.forEach(card => {{              /* <-- doubled braces! */
    let timer, startX, startY;

    /* cancel helper ---------------------------------------------------- */
    const cancel = () => clearTimeout(timer);

    /* start the timer on first contact --------------------------------- */
    card.addEventListener('pointerdown', e => {{
      startX = e.clientX;
      startY = e.clientY;
      timer  = setTimeout(() => toggleWatch(card), LONG);

    card.addEventListener('contextmenu', e => e.preventDefault());
    }});

    /* abort on any of these ------------------------------------------- */
    card.addEventListener('pointerup',     cancel);
    card.addEventListener('pointerleave',  cancel);
    card.addEventListener('pointercancel', cancel);         /* ← NEW */

    /* abort if the finger moves >10 px (scroll or drift) -------------- */
    card.addEventListener('pointermove', e => {{
      if (Math.abs(e.clientX - startX) > 10 ||
          Math.abs(e.clientY - startY) > 10) cancel();
    }});
  }});                                   /* <-- doubled braces! */

  /* ─────────────────── initial render + storage persistence ─────────────────── */
  apply();

  if (navigator.storage && navigator.storage.persist) {{
    navigator.storage.persist();
  }}

    /* ─────────────────── swipe to flip cinema faces ─────────────────── */
    /* inside your DOMContentLoaded handler — note the doubled {{ }} */
    cards.forEach(card => {{
      const faces = [...card.querySelectorAll('.face')];
      const pagerDots = [...card.querySelectorAll('.pager-dots span')];
      if (faces.length <= 1) return;       /* nothing to swipe */

      let idx = 0;                         /* which face is active */
      let x0, y0;
      const SWIPE_DIST   = 11;             /* px – horizontal threshold */
      const MAX_VERTICAL = 500;            /* px – allow some vertical drift */

      card.addEventListener('pointerdown', e => {{
        x0 = e.clientX;
        y0 = e.clientY;
        card.setPointerCapture(e.pointerId);
      }});

      card.addEventListener('pointerup', e => {{
        const dx = e.clientX - x0;
        const dy = e.clientY - y0;

        // only flip if you’ve moved enough horizontally,
        // and haven’t dragged too far vertically
        if (Math.abs(dx) >= SWIPE_DIST && Math.abs(dy) <= MAX_VERTICAL) {{
          faces[idx].classList.remove('active');
          idx = (dx < 0)
            ? (idx + 1) % faces.length
            : (idx - 1 + faces.length) % faces.length;
          faces[idx].classList.add('active');
          pagerDots.forEach(d => d.classList.remove('active'));
          pagerDots[idx].classList.add('active');
        }}

          // ← NEW: hide poster/title on any face ≠ 0
          if (idx > 0)  card.classList.add('alt-face');
          else          card.classList.remove('alt-face');

        card.releasePointerCapture(e.pointerId);
      }});

      card.addEventListener('pointercancel', e => {{
        card.releasePointerCapture(e.pointerId);
      }});
    }});

/* ---------- new helpers ---------- */
fillShowTimes();
wireFaceTabs();


}});
</script>




<div id="snackbar"></div>



</body></html>

"""

def _rel_str(show: dict) -> str:
    """Return the first *YYYY-MM-DD* we can find in show['releaseAt']."""
    rel = show.get("releaseAt")
    if isinstance(rel, str):               # already a plain string
        return rel
    if isinstance(rel, list):              # old format  ['2025-05-13', …]
        return rel[0] if rel else ""
    if isinstance(rel, dict):              # new format  {'NL_NL': '2025-07-30', …}
        return rel.get("NL_NL") or next(iter(rel.values()), "")
    return ""


def build_html(shows: List[dict],
               date: str,
               cinemas: Dict[str, Set[str]],
               zone_data: Dict[str, dict],
               cinema_showtimes: Dict[str, dict[str, dict]]) -> str:
    # ─── helper-wide reference date (avoid “maybe-defined” bugs) ───
    query_date     = dt.datetime.strptime(date, "%Y-%m-%d").date()
    formatted_date = query_date.strftime("%B %-d")

    # ─── helper: first show-time string for a given day ────────────────
    def _first_showtime_str(day_dict: dict) -> str:
        """Return 'HH:MM' of the first showtime, or '' if none."""
        try:
            t = day_dict.get("times", [])[0]       # '2025-05-30 15:00:00'
            return t[11:16]                        # slice HH:MM
        except Exception:
            return ""

    cards: List[str] = []
    for s in shows:
        # title—with language normalization and optional year suffix
        raw_title = s.get("title", "")

        # 1. Normalize language suffixes (case-insensitive)
        #    Replace any "(Originele versie)" or "(OV)" → "(EN)"
        #    and "(Nederlandse versie)" → "(NL)"
        raw_title = re.sub(r"\((?:Originele Versie|OV)\)", "(EN)", raw_title, flags=re.IGNORECASE)
        raw_title = re.sub(r"\((?:Nederlandse Versie)\)", "(NL)", raw_title, flags=re.IGNORECASE)
        raw_title = re.sub(r"\((?:Nederlands gesproken)\)", "(NL)", raw_title, flags=re.IGNORECASE)

        # 2. Robust release-date parsing – works for list / str / dict / None
        rel_field = s.get("releaseAt")

        if isinstance(rel_field, list) and rel_field:          # ["2025-11-14", …]
            rel_raw = rel_field[0]

        elif isinstance(rel_field, str):                       # "2025-11-14"
            rel_raw = rel_field

        elif isinstance(rel_field, dict):      # {"NL_NL": "..."}  or {"start": "..."}
            rel_raw = (
                rel_field.get("NL_NL")      # ← first look for NL locale
                or rel_field.get("start")
                or rel_field.get("releaseAt")
                or rel_field.get("date")
                or ""
            )

        else:                                                  # None / empty
            rel_raw = ""

        try:
            rel_date = dt.datetime.strptime(rel_raw[:10], "%Y-%m-%d").date()
            rel_year = rel_date.year
        except ValueError:
            rel_date = None
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
            # strip any “( … )” additions for a cleaner search query
            q = PAREN_RE.sub("", s.get("title", "")).strip()
            # optional but nice: URL-encode the query
            from urllib.parse import quote_plus
            href = f'https://www.imdb.com/find/?q={quote_plus(q)}'

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
        # Compute showtimes once, up front. Zone-based.
        next_showtimes = zd.get("zoneNext24", 0)

        # Now playing: next-showtimes + event
        if next_showtimes > 0:
            buttons.append(f'<span class="next-showtimes-button">{next_showtimes}x</span>')

        # Upcoming: either release-date (if not yet released) or next-showing (if already released)
        if next_showtimes == 0:
            rel = rel_raw
            if rel:
                show_date = dt.datetime.strptime(rel, "%Y-%m-%d").date()
                # (query_date is now computed once at the top)

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

                    is_avp = zd.get("hasAVP", False)
                    if first_upcoming and first_upcoming < show_date:
                        is_avp = True

                    # ① optional royal-blue “pre” button
                    if is_avp and first_upcoming:
                        pre_label = f"{first_upcoming.day} {first_upcoming.strftime('%b')}"
                        buttons.append(f'<span class="pre-button">{pre_label}</span>')

                    # ② always add the official release date badge
                    if show_date.year < query_date.year:
                        rel_label = str(show_date.year)
                    elif show_date.year > query_date.year:
                        rel_label = f"{show_date.day} {show_date.strftime('%b')} {str(show_date.year)[2:]}"
                    else:
                        rel_label = f"{show_date.day} {show_date.strftime('%b')}"

                    rel_cls = "release-date-button"
                    if bookable:
                        rel_cls += " bookable"
                    buttons.append(f'<span class="{rel_cls}">{rel_label}</span>')

        # Upcoming: bookable only if release date is in the future, otherwise Soon
        if next_showtimes == 0:
            # parse “today” from the outer date string
            today = query_date        # same object created at the top
            # grab the official releaseAt
            release_raw = rel_raw
            if release_raw:
                rel_date = dt.datetime.strptime(release_raw, "%Y-%m-%d").date()
            else:
                rel_date = today
            # only show “Book” when bookable AND release is strictly after today
            if bookable and rel_date > today:
                buttons.append(f'<span class="book-button">Pre</span>')
            # otherwise, if coming soon, show Soon
            elif s.get("isComingSoon"):
                buttons.append(f'<span class="soon-button">Soon</span>')

        # Runtime button
        if runtime and runtime != "1m":
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

        # Premium-format buttons (zone-scoped)
        if zd.get("hasIMAX"):
            buttons.append(
                '<a href="https://www.pathe.nl/nl/belevingen/imax" target="_blank" '
                'style="text-decoration:none;"><span class="event-button">IMAX</span></a>'
            )
        if zd.get("hasDolby"):
            buttons.append(
                '<a href="https://www.pathe.nl/nl/belevingen/dolby" target="_blank" '
                'style="text-decoration:none;"><span class="event-button">Dolby</span></a>'
            )
        if zd.get("isLast"):
            buttons.append(
                '<a href="https://www.pathe.nl/nl/belevingen/dolby" target="_blank" '
                'style="text-decoration:none;"><span class="event-button">LAST</span></a>'
            )

        # Leak alert
        if s.get("isLeaked"):
            buttons.append('<span class="web-button">Web</span>')

        # New-Bookable badge (orange, fading)
        if s.get("_is_new_bookable"):
            hours_ago = s["_hours_bookable"]
            days_ago  = int(hours_ago // 24)
            alpha     = max(0.3, 1 - 0.7 * (hours_ago / NEW_BOOKABLE_HOURS))
            buttons.append(
                f'<span class="new-bookable-button" style="--alpha:{alpha:.2f}">'
                f'{days_ago}day</span>'
            )

        # New-Soon badge (purple, constant)
        if s.get("_is_new_soon"):
            buttons.append('<span class="new-soon-button">New</span>')

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

        # gather tag keywords for the quick-filter
        tag_keys = []

        # 1️⃣ NOW – at least one Den Haag showtime in the next 24 h
        zone_next24 = zd.get("zoneNext24", 0)
        if zone_next24 > 0:
            tag_keys.append("now")
        else:
            # 2️⃣ BOOK – not “now”, bookable == True
            if zd.get("bookable"):
                tag_keys.append("book")
            # 3️⃣ SOON – not “now”, not “book”, but marked coming-soon
            elif s.get("isComingSoon"):
                tag_keys.append("soon")

        # Kids first so the order in the attribute mirrors the toolbar
        if is_kids:
            tag_keys.append("kids")

        # Recent (bookable OR soon)
        if s.get("_is_new_bookable") or s.get("_is_new_soon"):
            tag_keys.append("new")

        # Premium formats
        if zd.get("hasDolby"):
            tag_keys.append("dolby")
        if zd.get("hasIMAX"):
            tag_keys.append("imax")

        # IMDB 7 +
        try:
            if float(s.get("omdbRating") or 0) >= 7.0:
                tag_keys.append("imdb7")
        except ValueError:
            pass

        # Leaked titles
        if s.get("isLeaked"):
            tag_keys.append("web")

        # 4️⃣ FUTURE – no showtimes, not bookable, not coming soon
        if zone_next24 == 0 and not bookable and not s.get("isComingSoon"):
            tag_keys.append("future")

        # ───── swipe-faces (main + one aggregated showtimes face) ─────
        faces  = []
        face_id = 0

        # ① main face (always there)
        faces.append(
            f'<div class="face active" data-face="{face_id}">'
            f'{ratings_html}{theaters_html}{buttons_line}'
            f'</div>'
        )

        # ② only build face 1 if at least one cinema really has ≥1 showtime on {date}
        face_id += 1
        blocks = []
        for cin_slug, cin_name in FAV_CINEMAS:
            # look up that cinema’s fetched showtimes for this slug on exactly “date”
            day_info = (
                cinema_showtimes
                .get(cin_slug, {})        # might not exist if cinema never fetched
                .get(slug, {})            # might not exist if slug never fetched
                .get("days", {})          # the “days” dictionary
                .get(date, {})            # the specific day we care about
            )
            times = day_info.get("showtimes", [])
            if not times:
                # either this film isn’t in that cinema *or* there really are no showtimes
                continue

            code = cin_name[:2].upper()
            blocks.append(
                f'<div class="cinema-block">'
                f'  <span class="cinema-logo" data-code="{code}">'
                f'{escape(cin_name)}</span>'
                f'  <div class="buttons-line"></div>'
                f'</div>'
            )

        # only append the “face 1” wrapper if at least one cinema-block was built:
        if blocks:
            faces.append(
                f'<div class="face" data-face="{face_id}">'
                + "".join(blocks) +
                "</div>"
            )

        faces_html = '<div class="faces">' + "".join(faces) + '</div>'


        # ---------- page-indicator dots ----------
        num_faces = len(faces)            # face 0 + any extras
        dots_html = ""
        if num_faces > 1:                 # only when there is something to swipe
            dots = ['<span class="{}"></span>'.format("active" if i == 0 else "")
                    for i in range(num_faces)]
            dots_html = '<div class="pager-dots">' + "".join(dots) + '</div>'   

        # ---------- final card ----------
        hidden_cls = " hidden" if "future" in tag_keys else ""
        tags_str   = " ".join(tag_keys)

        cards.append(
            f'<div class="card{hidden_cls}" data-tags="{tags_str}" data-slug="{slug}">'
            f'{img}'
            f'<div class="card-body">{title_html}{faces_html}{dots_html}</div>'
            f'</div>'
        )


    now = dt.datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d %H:%M %Z")

    # escape any stray “{ }” that may appear inside movie titles etc.
    cards_html = "\n    ".join(cards)


    # -------- collect show-times for all cards (only for *today*) ----------
    show_times_map: dict[str, dict[str, list[str]]] = {}
    for cin_slug, cin_name in FAV_CINEMAS:
        code = cin_name[:2].upper()
        for slug, entry in cinema_showtimes.get(cin_slug, {}).items():
            day = entry.get("days", {}).get(date, {})
            times = day.get("showtimes", [])
            if times:
                show_times_map.setdefault(slug, {})[code] = times

    show_times_json = json.dumps(show_times_map, separators=(',', ':'))

    return HTML_TMPL.format(
        date=date,
        css=MOBILE_CSS,
        cards=cards_html,
        now=now,
        formatted_date=formatted_date,
        show_times=show_times_json          # ← NEW
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

    # ───── Merge zone metadata ───────────────────────────────────────
    zone_shows = fetch_zone_shows()
    for s in shows:                       # enrich the “global” entries first
        slug = s.get("slug")
        if slug in zone_shows:
            s.update(zone_shows[slug])

    # ───── Single-show enrichment & stub pickup (cache-bust) ────────
    def enrich_or_fetch(slug: str, existing: Optional[dict]) -> Optional[dict]:
        """
        • If *existing* is provided → mutate it in-place via _merge_single_record.
        • If *existing* is None       → try to fetch a full record for slug.
        Returns the (possibly new) record or None.
        """
        try:
            if existing is not None:
                return _merge_single_record(existing)           # in-place merge
            else:                                               # zone-only stub
                url  = f"https://www.pathe.nl/api/show/{slug}"
                data = get_json(url, params={"language": "nl"})
                return _merge_single_record(data) if (data.get("title") or "").strip() else None
        except Exception as exc:
            LOG.debug("single-show fetch/merge failed for %s – %s", slug, exc)
            return None

    # Map slug → current dict (or None if we don't have it yet)
    slug_to_obj = {s["slug"]: s for s in shows}
    for zslug in zone_shows:
        slug_to_obj.setdefault(zslug, None)                    # inject stubs

    LOG.info("🔗 cache-busting %d titles via single-show endpoint …", len(slug_to_obj))
    with cf.ThreadPoolExecutor(max_workers=10) as px:
        futs = {px.submit(enrich_or_fetch, slug, obj): slug for slug, obj in slug_to_obj.items()}
        shows = []                                             # rebuild list
        for fut in cf.as_completed(futs):
            rec = fut.result()
            if rec:
                shows.append(rec)

    # recent-film metadata  (adds _hours_ago  &  _is_recent)
    for s in shows:
        hrs, recent = register_and_age(s["slug"])
        s["_hours_ago"] = hrs
        s["_is_recent"] = recent

    # Fetch zone data (isKids and other flags)
    zone_data = fetch_zone_data()
    
    # ─── track “new” states (bookable & soon) ─────────────────────────
    for s in shows:
        slug = s["slug"]
        zd   = zone_data.get(slug, {})

        # a) bookable-based “0day/n-day” badge
        hrs_b, is_new_b = register_and_age_bookable(slug, zd.get("bookable", False))
        s["_hours_bookable"]  = hrs_b
        s["_is_new_bookable"] = is_new_b

        # b) coming-soon “New” badge
        hrs_s, is_new_s = register_and_age_soon(slug, s.get("isComingSoon", False))
        s["_hours_soon"]     = hrs_s
        s["_is_new_soon"]    = is_new_s

    # cinema presence + full showtimes map
    cinemas: Dict[str, Set[str]] = {}
    cinema_showtimes: Dict[str, dict[str, dict]] = {}
    for slug, _ in FAV_CINEMAS:
        # grab exactly those movies playing on args.date
        cinemas[slug] = fetch_cinema_slugs(slug, args.date)
        # still fetch the full multi-day schedule for your “next-showing” button logic
        cinema_showtimes[slug] = fetch_cinema_showtimes_data(slug, args.date)

    # OMDb  (daily cache; API key optional)
    enrich_with_omdb(shows, None if args.skip_omdb else key)

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
        cnt = zone_data.get(s["slug"], {}).get("zoneNext24", 0)
        # 0️⃣ Now playing → highest count first
        if cnt > 0:
            return (0, -cnt, dt.date.min)

        # ─── Robust “official” releaseAt parsing ───────────────────
        rel_field = _rel_str(s)         # may be list | str | dict | None

        if isinstance(rel_field, list) and rel_field:
            raw = rel_field[0]

        elif isinstance(rel_field, str):
            raw = rel_field

        elif isinstance(rel_field, dict):
            raw = rel_field.get("start") or rel_field.get("releaseAt") or rel_field.get("date") or ""

        else:
            raw = ""

        try:
            release_date = dt.datetime.strptime(raw[:10], "%Y-%m-%d").date()
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

