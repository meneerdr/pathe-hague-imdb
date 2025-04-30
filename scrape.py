#!/usr/bin/env python3
"""
Fetch films for every Pathé Den Haag theatre via Pathé’s JSON API,
rank by IMDb via OMDb, and write index.html.
"""

from pathlib import Path
from datetime import datetime
import os, time, requests, pandas as pd
import urllib.parse, re

# ––– Config ––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
API_KEY = os.getenv("OMDB_KEY")
if not API_KEY:
    raise SystemExit("❌  export OMDB_KEY=<your-key> before running")

THEATERS = {
    "Pathé Buitenhof":    101,
    "Pathé Spuimarkt":    102,
    "Pathé Scheveningen": 103,
    "Pathé Ypenburg":     130,
}
PATHÉ_API = "https://www.pathe.nl/api/v2/movies"  # public REST used by pathe.nl

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17 Safari/605.1.15",
    "Accept": "application/json"
})

# ––– 1. Get film titles per theatre ––––––––––––––––––––––––––––––––––––
def fetch_titles():
    films = {}
    for name, tid in THEATERS.items():
        params = {
            "theaterIds": tid,
            "playingNow": "true",
            "include": "theaters",
            "pageSize": 50
        }
        r = session.get(PATHÉ_API, params=params, timeout=20).json()
        for item in r.get("items", []):
            title = item["title"].strip()
            release = item.get("releaseDate", "")
            entry = films.setdefault(title, {"next": "", "theaters": set()})
            entry["theaters"].add(name)
            if release and (not entry["next"] or release < entry["next"]):
                entry["next"] = datetime.strptime(release, "%Y-%m-%d").strftime("%d-%m-%Y")
    return films

# ––– 2. OMDb lookup ––––––––––––––––––––––––––––––––––––––––––––––––––––
def omdb(title):
    base = "https://www.omdbapi.com/"
    for query in ({"t": title}, {"s": title}):
        r = session.get(base, params={**query, "apikey": API_KEY, "plot": "short", "r": "json"}, timeout=20).json()
        if r.get("Response") == "True":
            if query.get("s"):  # search path, take first match
                imdb_id = r["Search"][0]["imdbID"]
                r = session.get(base, params={"i": imdb_id, "apikey": API_KEY, "plot": "short", "r": "json"}, timeout=20).json()
            return r
    return {}  # not found

# ––– 3. Build dataframe ––––––––––––––––––––––––––––––––––––––––––––––––
def build_table(films):
    rows = []
    for title, meta in films.items():
        data = omdb(title)
        rows.append({
            "Film": title,
            "IMDb": float(data["imdbRating"]) if data.get("imdbRating","N/A") != "N/A" else None,
            "Genre": (data.get("Genre","").split(",")[0]).strip(),
            "Actors": (data.get("Actors","").split(",")[0]).strip(),
            "Plot": (data.get("Plot","")[:110] + "…") if len(data.get("Plot","")) > 110 else data.get("Plot",""),
            "Next": meta["next"],
            "Theaters": ", ".join(sorted(meta["theaters"]))
        })
        time.sleep(0.15)   # OMDb free-tier
    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit("❌  Pathé API returned no films; aborting.")
    return df.sort_values("IMDb", ascending=False, na_position="last")

# ––– 4. Inject into template.html ––––––––––––––––––––––––––––––––––––––
def inject_html(df):
    def li(r):
        rating = f"{r.IMDb:.1f}" if pd.notna(r.IMDb) else "n.v.t."
        meta   = f'{r.Genre} • {r.Theaters}'
        coming = f'<span class="coming">Vanaf {r.Next}</span>' if r.Next else ""
        return (
            f'<li class="card"><div class="rating">{rating}</div><div class="info">'
            f'<h2>{r.Film}</h2><div class="meta">{meta}</div>'
            f'<p class="plot">{r.Plot} {coming}</p></div></li>'
        )

    rows_html = "\n".join(df.apply(li, axis=1))
    tpl       = Path("template.html").read_text(encoding="utf-8")
    html      = re.sub(r"<!-- MOVIE_ROWS.*?-->", "<!-- MOVIE_ROWS -->\n"+rows_html,
                       tpl, flags=re.S)
    html      = html.replace("{{DATE}}",
                             datetime.now().strftime("%d-%m-%Y %H:%M"))
    Path("index.html").write_text(html, encoding="utf-8")
    print(f"✅ index.html generated ({len(df)} films)")

# ––– Main ––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
if __name__ == "__main__":
    print("🔗 Querying Pathé JSON API…")
    movies = fetch_titles()
    print(f"   {len(movies)} unique titles found.")
    df = build_table(movies)
    inject_html(df)

