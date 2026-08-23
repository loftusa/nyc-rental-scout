#!/usr/bin/env python3
"""Daily refresh pipeline for the NYC rental scout.

Forked from alex-loftus.com/houses (Bay Area rental scout) and reconfigured
for New York City. All personal data stripped; everything you should tweak
lives in the CONFIG block below.

Three modes:
  --pull   Pull live Craigslist rentals (sapi JSON API) + Rent.com complexes
           (best-effort), dedupe, filter to zone/budget, attach neighborhood
           priors + commute estimates, select ~55 diverse candidates, scrape
           each one's photo gallery AND posting body text
           -> writes refresh/shortlist.json.
           Fails loud (non-zero exit) if Craigslist returns too little data.
  --build  Merge shortlist.json + ratings.json (written by rate.py) with the
           commute model -> writes ../data.js (window.HOUSES_DATA).
  --sweep  Keyless freshness pass: re-check each shown listing's page and
           prune dead ones. No LLM, no API key.

Usage:
    python3 refresh/refresh.py --pull
    python3 refresh/rate.py            # needs ANTHROPIC_API_KEY
    python3 refresh/refresh.py --build
"""
import argparse
import collections
import datetime
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

# --------------------------------------------------------------------------- #
# CONFIG — edit this block for your own search
# --------------------------------------------------------------------------- #

# Commute anchors: where you need to get to. ANCHOR_A is required (work,
# school, ...). ANCHOR_B is optional — set to None for single-anchor mode,
# or e.g. ("Downtown Brooklyn", "Bk", 40.6928, -73.9866) to reward places
# reachable to BOTH. Format: (label, short_label, lat, lon).
ANCHOR_A = ("Midtown Manhattan", "Mid", 40.7549, -73.9840)
ANCHOR_B = None

# Budget (USD/month). "apt" = whole apartments/studios, "room" = rooms/shares.
APT_MIN, APT_MAX = 1500, 4000
ROOM_MIN, ROOM_MAX = 900, 2300
APT_TARGET = 3000  # price_fit sweet spot for apartments
ROOM_TARGET = 1500  # price_fit sweet spot for rooms

MAX_MILES_FROM_ANCHOR = 13  # drop listings farther than this from ANCHOR_A
SHORTLIST_SIZE = 55

# Commute scoring: minutes <= COMMUTE_PERFECT score 10; 0 at COMMUTE_ZERO.
COMMUTE_PERFECT = 25
COMMUTE_ZERO = 65

# Neighborhoods you love get a +0.8 fit bonus and a heart in the UI.
# Lowercase substrings matched against the listing's neighborhood string.
LOVED = ()

# Craigslist search centers: (name, lat, lon, radius_mi). These cover the
# five boroughs + Hudson-county NJ; trim or extend for your search zone.
CENTERS = [
    ("manhattan_upper", 40.79, -73.955, 4),  # UWS/UES/Harlem/Wash Heights
    ("manhattan_lower", 40.73, -73.99, 3),  # Village/LES/Chelsea/FiDi
    ("brooklyn_north", 40.695, -73.94, 5),  # Wburg/Bushwick/BedStuy/Slope
    ("brooklyn_south", 40.63, -73.98, 6),  # Sunset Pk/Bay Ridge/Flatbush
    ("queens_west", 40.75, -73.90, 5),  # Astoria/LIC/Sunnyside/JH
    ("bronx", 40.85, -73.89, 5),
    ("nj_hudson", 40.73, -74.05, 4),  # Jersey City/Hoboken
]
# (craigslist category, min_price, max_price, bucket)
QUERIES = [
    ("apa", APT_MIN, APT_MAX, "apt"),
    ("roo", ROOM_MIN, ROOM_MAX, "room"),
]

# Rent.com city pages (managed complexes; best-effort — a blocked source
# only warns). Check these URLs in a browser if they contribute nothing.
RENT_URLS = [
    ("nyc", f"https://www.rent.com/new-york/new-york-apartments/max-price-{APT_MAX}"),
    (
        "brooklyn",
        f"https://www.rent.com/new-york/brooklyn-apartments/max-price-{APT_MAX}",
    ),
    ("queens", f"https://www.rent.com/new-york/queens-apartments/max-price-{APT_MAX}"),
    (
        "jersey-city",
        f"https://www.rent.com/new-jersey/jersey-city-apartments/max-price-{APT_MAX}",
    ),
]

# OSM bounding box for the gym proximity model (south,west,north,east).
GYM_BBOX = "(40.55,-74.10,40.95,-73.70)"

# Fit weights — shipped to the frontend via meta.fit_weights (the sliders'
# defaults). Keys: nice, nature, soft (quiet for apts / social for rooms),
# value, commute, aesthetic, gym. Must sum to ~1.
WEIGHTS = {
    "nice": 0.16,
    "nature": 0.10,
    "soft": 0.14,
    "value": 0.16,
    "commute": 0.26,
    "aesthetic": 0.12,
    "gym": 0.06,
}

# How many shortlist slots per region (sums to ~SHORTLIST_SIZE).
REGION_TARGET = {
    "Manhattan": 14,
    "Brooklyn": 16,
    "Queens": 12,
    "Bronx": 4,
    "NJ": 6,
    "Other": 3,
}

# --------------------------------------------------------------------------- #
# Neighborhood priors — seed estimates only; the LLM rater refines each
# listing. keyword -> (region, transit_min_to_ANCHOR_A, nature, quiet, nice,
# social) with the last four on a 0-5 scale. Matched top-down as substrings
# of the lowercased Craigslist neighborhood string, so put specific names
# (e.g. "east village") before generic ones ("village", "manhattan").
# Transit minutes assume ANCHOR_A = Midtown; if you move the anchor far,
# the distance-based fallback model still keeps rankings sane.
# --------------------------------------------------------------------------- #
RULES = [
    # Manhattan
    ("battery park", ("Manhattan", 25, 3, 3, 4, 2)),
    ("financial district", ("Manhattan", 22, 1, 2, 4, 3)),
    ("fidi", ("Manhattan", 22, 1, 2, 4, 3)),
    ("tribeca", ("Manhattan", 18, 2, 3, 5, 3)),
    ("soho", ("Manhattan", 15, 1, 2, 5, 4)),
    ("nolita", ("Manhattan", 15, 1, 2, 4, 4)),
    ("little italy", ("Manhattan", 16, 1, 2, 3, 3)),
    ("two bridges", ("Manhattan", 20, 1, 2, 2, 3)),
    ("chinatown", ("Manhattan", 18, 1, 2, 2, 3)),
    ("lower east side", ("Manhattan", 18, 1, 1, 3, 5)),
    ("east village", ("Manhattan", 15, 2, 2, 4, 5)),
    ("west village", ("Manhattan", 14, 2, 3, 5, 4)),
    ("greenwich village", ("Manhattan", 13, 2, 3, 5, 4)),
    ("alphabet city", ("Manhattan", 20, 2, 2, 3, 4)),
    ("union square", ("Manhattan", 10, 1, 2, 4, 4)),
    ("gramercy", ("Manhattan", 10, 2, 3, 4, 3)),
    ("stuyvesant town", ("Manhattan", 15, 3, 4, 4, 2)),
    ("stuy town", ("Manhattan", 15, 3, 4, 4, 2)),
    ("kips bay", ("Manhattan", 10, 1, 3, 3, 3)),
    ("murray hill", ("Manhattan", 8, 1, 3, 4, 4)),
    ("flatiron", ("Manhattan", 8, 1, 2, 4, 4)),
    ("chelsea", ("Manhattan", 8, 2, 3, 5, 4)),
    ("hudson yards", ("Manhattan", 8, 2, 3, 4, 2)),
    ("hell's kitchen", ("Manhattan", 6, 1, 2, 3, 4)),
    ("hells kitchen", ("Manhattan", 6, 1, 2, 3, 4)),
    ("clinton", ("Manhattan", 6, 1, 2, 3, 4)),
    ("midtown", ("Manhattan", 4, 1, 1, 3, 2)),
    ("times square", ("Manhattan", 4, 1, 1, 2, 2)),
    ("theater district", ("Manhattan", 4, 1, 1, 3, 2)),
    ("upper west side", ("Manhattan", 12, 4, 4, 5, 3)),
    ("upper west", ("Manhattan", 12, 4, 4, 5, 3)),
    ("uws", ("Manhattan", 12, 4, 4, 5, 3)),
    ("upper east side", ("Manhattan", 14, 3, 4, 5, 2)),
    ("upper east", ("Manhattan", 14, 3, 4, 5, 2)),
    ("ues", ("Manhattan", 14, 3, 4, 5, 2)),
    ("yorkville", ("Manhattan", 18, 3, 4, 4, 2)),
    ("lenox hill", ("Manhattan", 12, 2, 4, 5, 2)),
    ("roosevelt island", ("Manhattan", 16, 4, 5, 4, 1)),
    ("morningside", ("Manhattan", 20, 4, 3, 4, 3)),
    ("manhattan valley", ("Manhattan", 16, 3, 3, 3, 3)),
    ("east harlem", ("Manhattan", 20, 2, 2, 2, 3)),
    ("hamilton heights", ("Manhattan", 25, 3, 3, 3, 3)),
    ("sugar hill", ("Manhattan", 26, 3, 3, 3, 2)),
    ("washington heights", ("Manhattan", 30, 4, 3, 3, 3)),
    ("inwood", ("Manhattan", 35, 5, 4, 3, 2)),
    ("harlem", ("Manhattan", 22, 2, 2, 3, 4)),
    # Brooklyn
    ("dumbo", ("Brooklyn", 18, 3, 3, 5, 3)),
    ("brooklyn heights", ("Brooklyn", 18, 3, 4, 5, 2)),
    ("cobble hill", ("Brooklyn", 22, 3, 4, 5, 2)),
    ("carroll gardens", ("Brooklyn", 24, 3, 4, 5, 3)),
    ("boerum hill", ("Brooklyn", 20, 2, 3, 4, 3)),
    ("gowanus", ("Brooklyn", 25, 2, 3, 3, 4)),
    ("park slope", ("Brooklyn", 25, 4, 4, 5, 3)),
    ("prospect heights", ("Brooklyn", 24, 4, 3, 4, 4)),
    ("prospect lefferts", ("Brooklyn", 30, 4, 3, 3, 3)),
    ("fort greene", ("Brooklyn", 20, 4, 3, 4, 4)),
    ("clinton hill", ("Brooklyn", 22, 3, 3, 4, 4)),
    ("downtown brooklyn", ("Brooklyn", 18, 1, 2, 3, 3)),
    ("williamsburg", ("Brooklyn", 18, 2, 2, 4, 5)),
    ("greenpoint", ("Brooklyn", 25, 3, 3, 4, 5)),
    ("bushwick", ("Brooklyn", 28, 1, 2, 2, 5)),
    ("bedford-stuyvesant", ("Brooklyn", 26, 2, 2, 3, 4)),
    ("bedford stuyvesant", ("Brooklyn", 26, 2, 2, 3, 4)),
    ("bed-stuy", ("Brooklyn", 26, 2, 2, 3, 4)),
    ("bed stuy", ("Brooklyn", 26, 2, 2, 3, 4)),
    ("crown heights", ("Brooklyn", 30, 3, 3, 3, 4)),
    ("flatbush", ("Brooklyn", 35, 3, 3, 3, 3)),
    ("ditmas park", ("Brooklyn", 35, 4, 4, 4, 3)),
    ("kensington", ("Brooklyn", 35, 3, 4, 3, 2)),
    ("windsor terrace", ("Brooklyn", 30, 4, 4, 4, 2)),
    ("sunset park", ("Brooklyn", 35, 3, 3, 3, 3)),
    ("bay ridge", ("Brooklyn", 45, 3, 4, 4, 2)),
    ("bensonhurst", ("Brooklyn", 50, 2, 4, 3, 1)),
    ("sheepshead", ("Brooklyn", 50, 3, 4, 3, 1)),
    ("brighton beach", ("Brooklyn", 55, 4, 3, 3, 1)),
    ("coney island", ("Brooklyn", 55, 4, 3, 2, 1)),
    ("east new york", ("Brooklyn", 40, 1, 2, 1, 1)),
    ("brownsville", ("Brooklyn", 40, 1, 2, 1, 1)),
    ("canarsie", ("Brooklyn", 50, 2, 3, 2, 1)),
    ("greenwood", ("Brooklyn", 30, 3, 4, 3, 2)),
    ("red hook", ("Brooklyn", 35, 3, 3, 3, 3)),
    # Queens
    ("long island city", ("Queens", 12, 2, 3, 4, 3)),
    ("lic", ("Queens", 12, 2, 3, 4, 3)),
    ("hunters point", ("Queens", 14, 3, 3, 4, 2)),
    ("astoria", ("Queens", 20, 2, 3, 4, 4)),
    ("ditmars", ("Queens", 25, 3, 3, 4, 3)),
    ("sunnyside", ("Queens", 18, 2, 4, 4, 3)),
    ("woodside", ("Queens", 22, 2, 3, 3, 2)),
    ("jackson heights", ("Queens", 25, 2, 3, 3, 3)),
    ("elmhurst", ("Queens", 28, 2, 3, 3, 2)),
    ("maspeth", ("Queens", 30, 2, 4, 3, 1)),
    ("middle village", ("Queens", 35, 3, 4, 3, 1)),
    ("ridgewood", ("Queens", 30, 2, 3, 3, 4)),
    ("rego park", ("Queens", 30, 2, 4, 3, 1)),
    ("forest hills", ("Queens", 30, 4, 4, 4, 1)),
    ("kew gardens", ("Queens", 35, 3, 4, 3, 1)),
    ("flushing", ("Queens", 40, 2, 3, 3, 2)),
    ("corona", ("Queens", 32, 2, 3, 2, 2)),
    ("jamaica", ("Queens", 40, 1, 2, 2, 1)),
    ("bayside", ("Queens", 50, 4, 4, 4, 1)),
    ("far rockaway", ("Queens", 65, 4, 3, 2, 1)),
    ("rockaway", ("Queens", 60, 4, 3, 3, 2)),
    # Bronx
    ("mott haven", ("Bronx", 20, 1, 2, 2, 2)),
    ("south bronx", ("Bronx", 22, 1, 2, 2, 2)),
    ("hunts point", ("Bronx", 25, 1, 2, 1, 1)),
    ("concourse", ("Bronx", 25, 2, 2, 2, 2)),
    ("fordham", ("Bronx", 35, 2, 2, 2, 2)),
    ("belmont", ("Bronx", 35, 2, 3, 3, 2)),
    ("kingsbridge", ("Bronx", 35, 3, 3, 3, 1)),
    ("riverdale", ("Bronx", 40, 5, 5, 4, 1)),
    ("pelham", ("Bronx", 40, 3, 4, 3, 1)),
    ("throgs neck", ("Bronx", 50, 3, 4, 3, 1)),
    ("bronx", ("Bronx", 32, 2, 2, 2, 2)),
    # NJ (PATH-adjacent Hudson county)
    ("hoboken", ("NJ", 25, 2, 3, 4, 4)),
    ("journal square", ("NJ", 25, 1, 3, 3, 2)),
    ("jersey city heights", ("NJ", 35, 2, 3, 3, 2)),
    ("the heights", ("NJ", 35, 2, 3, 3, 2)),
    ("grove st", ("NJ", 22, 2, 3, 4, 3)),
    ("newport", ("NJ", 20, 2, 3, 4, 2)),
    ("paulus hook", ("NJ", 22, 3, 3, 4, 2)),
    ("greenville", ("NJ", 40, 1, 3, 2, 1)),
    ("jersey city", ("NJ", 28, 2, 3, 3, 3)),
    ("weehawken", ("NJ", 30, 3, 4, 3, 1)),
    ("union city", ("NJ", 35, 1, 3, 2, 1)),
    ("west new york", ("NJ", 40, 2, 3, 2, 1)),
    # generic catch-alls (keep last)
    ("staten island", ("Other", 60, 4, 4, 3, 1)),
    ("brooklyn", ("Brooklyn", 30, 2, 3, 3, 3)),
    ("queens", ("Queens", 30, 2, 3, 3, 2)),
    ("manhattan", ("Manhattan", 15, 1, 2, 3, 3)),
    ("new york", ("Manhattan", 15, 1, 2, 3, 3)),
]
DEFAULT_PRIOR = ("Other", 35, 2, 3, 3, 2)

# --------------------------------------------------------------------------- #
# end CONFIG
# --------------------------------------------------------------------------- #

HERE = os.path.dirname(os.path.abspath(__file__))
SHORTLIST = os.path.join(HERE, "shortlist.json")
RATINGS = os.path.join(HERE, "ratings.json")
PULL_STATS = os.path.join(HERE, "pull_stats.json")
DATA_JS = os.path.abspath(os.path.join(HERE, "..", "data.js"))
GYMS_JSON = os.path.join(HERE, "gyms.json")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
SAPI = "https://sapi.craigslist.org/web/v8/postings/search/full"

WALK_FACTOR = 1.3 * 20  # straight-line mi -> walk minutes (route factor x 20 min/mi)

# Minimum kept listings before we trust a pull; below this we assume the IP
# got blocked/rate-limited and refuse to build a degraded page.
MIN_KEPT = 30
MIN_SHOWN = 15

IMG_RE = re.compile(
    r"https://images\.craigslist\.org/[A-Za-z0-9]+_[A-Za-z0-9]+_[A-Za-z0-9]+_600x450\.jpg"
)
DEAD_RE = re.compile(
    r"This posting has been (deleted|flagged)|has expired|<title>[^<]*(removed|deleted)",
    re.I,
)
BODY_RE = re.compile(r'<section id="postingbody">(.*?)</section>', re.S)
TAG_RE = re.compile(r"<[^>]+>")

assert abs(sum(WEIGHTS.values()) - 1.0) < 0.05, "WEIGHTS should sum to ~1"
assert ANCHOR_A and len(ANCHOR_A) == 4, "ANCHOR_A = (label, short, lat, lon)"
assert (
    ANCHOR_B is None or len(ANCHOR_B) == 4
), "ANCHOR_B = (label, short, lat, lon) or None"


# --------------------------------------------------------------------------- #
# --pull
# --------------------------------------------------------------------------- #
def haversine_mi(lat1, lon1, lat2, lon2):
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def curl_json(url):
    out = subprocess.run(
        [
            "curl",
            "-sS",
            "--max-time",
            "30",
            "-A",
            UA,
            "-H",
            "Accept: application/json",
            url,
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0 or not out.stdout:
        print(
            f"  curl failed rc={out.returncode} err={out.stderr[:120]}", file=sys.stderr
        )
        return None
    try:
        return json.loads(out.stdout)
    except Exception as e:
        print(f"  json parse failed: {e}", file=sys.stderr)
        return None


def parse_item(it, locs):
    pid = it[0]
    price = it[3] if len(it) > 3 else None
    geo = it[4] if len(it) > 4 else ""
    lat = lon = hood = None
    if isinstance(geo, str) and "~" in geo:
        p = geo.split("~")
        lp = p[0].split(":")
        if len(lp) > 1:
            try:
                hi = int(lp[1])
                if 0 < hi < len(locs):
                    hood = locs[hi]
            except Exception:
                pass
        try:
            lat = float(p[1])
            lon = float(p[2])
        except Exception:
            pass
    title = slug = token = pdisp = beds = None
    imgs = []
    for el in it[6:]:
        if isinstance(el, str):
            title = el
        elif isinstance(el, list) and el:
            t = el[0]
            if t == 4:
                imgs = el[1:]
            elif t == 6:
                slug = el[1] if len(el) > 1 else None
            elif t == 10:
                pdisp = el[1] if len(el) > 1 else None
            elif t == 13:
                token = el[1] if len(el) > 1 else None
            elif t == 5:
                beds = el[1] if len(el) > 1 else None
    img = None
    if imgs:
        ref = imgs[0]
        if isinstance(ref, str) and ":" in ref:
            core = ref.split(":", 1)[1]  # keep host suffix: 00T0T_8I6TRnex3fH_0nm0hw
            img = f"https://images.craigslist.org/{core}_600x450.jpg"
    url = (
        f"https://www.craigslist.org/view/d/{slug}/{token}" if slug and token else None
    )
    if beds is None and isinstance(title, str):
        m = re.search(r"(\d)\s*br", title.lower())
        if m:
            beds = int(m.group(1))
        elif "studio" in title.lower():
            beds = 0
    return dict(
        pid=pid,
        price=price,
        pdisp=pdisp,
        beds=beds,
        hood=hood,
        lat=lat,
        lon=lon,
        title=title,
        url=url,
        img=img,
        nimg=len(imgs),
        src="cl",
    )


def pull_raw(centers=CENTERS, queries=QUERIES):
    seen = {}
    for cname, lat, lon, dist in centers:
        for cat, pmin, pmax, bucket in queries:
            params = {
                "batch": "1-0-360-0-0",
                "cc": "US",
                "lang": "en",
                "searchPath": cat,
                "min_price": pmin,
                "max_price": pmax,
                "lat": lat,
                "lon": lon,
                "search_distance": dist,
                "sort": "date",
                "availabilityMode": 0,
            }
            url = SAPI + "?" + urllib.parse.urlencode(params)
            d = curl_json(url)
            if not d:
                print(f"[{cname}/{cat}] FAILED")
                continue
            data = d["data"]
            locs = data["decode"]["locationDescriptions"]
            items = data["items"]
            n_new = 0
            for raw in items:
                r = parse_item(raw, locs)
                if not r["url"] or r["price"] is None:
                    continue
                r["bucket"] = bucket
                if r["pid"] not in seen:
                    seen[r["pid"]] = r
                    n_new += 1
            print(
                f"[{cname}/{cat}] pulled {len(items)}; +{n_new} new; unique={len(seen)}"
            )
            time.sleep(1.2)  # be polite
    return list(seen.values())


# --------------------------------------------------------------------------- #
# extra sources (non-Craigslist). Craigslist is the backbone; everything here
# is best-effort — a blocked/broken source warns and contributes nothing.
# --------------------------------------------------------------------------- #
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
RENT_IMG = "https://i.rent.com/t_3x2_fixed_webp_lg/{}"


def curl_text(url):
    out = subprocess.run(
        ["curl", "-sSL", "--max-time", "45", "-A", UA, url],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0 or not out.stdout:
        print(
            f"  curl failed rc={out.returncode} err={out.stderr[:120]}", file=sys.stderr
        )
        return None
    return out.stdout


def parse_rent_listing(lst):
    """One Rent.com __NEXT_DATA__ listing -> normalized row (None = unusable)."""
    loc = lst.get("location") or {}
    lat, lon = loc.get("lat"), loc.get("lng")
    price = lst.get("price")
    imgs = [
        RENT_IMG.format(p["id"])
        for p in (lst.get("optimizedPhotos") or [])
        if p.get("id")
    ][:24]
    if not (lat and lon and lst.get("urlPathname")):
        return None
    if not isinstance(price, (int, float)) or not 100 < price < 20000:
        return None
    if not imgs:
        return None
    bcd = [
        b
        for b in (lst.get("bedCountData") or [])
        if isinstance((b.get("prices") or {}).get("low"), (int, float))
    ]
    beds = min(bcd, key=lambda b: b["prices"]["low"])["beds"] if bcd else None
    name = lst.get("name") or ""
    addr = lst.get("addressFull") or ""
    parts = ["Managed apartment complex (via Rent.com)."]
    facts = " · ".join(
        x
        for x in (
            lst.get("bedText"),
            lst.get("bathText"),
            lst.get("squareFeetText"),
            lst.get("unitsAvailableText"),
        )
        if x
    )
    if facts:
        parts.append(facts + ".")
    amenities = lst.get("amenitiesHighlighted") or []
    if amenities:
        parts.append("Amenities: " + ", ".join(amenities[:10]) + ".")
    if lst.get("phoneDesktopText"):
        parts.append(f"Leasing office: {lst['phoneDesktopText']}.")
    return dict(
        pid=lst.get("id"),
        price=int(price),
        pdisp=lst.get("priceText"),
        beds=beds,
        bucket="apt",
        hood=(loc.get("city") or "").lower() or None,
        lat=lat,
        lon=lon,
        title=f"{name} — {addr}" if addr else name,
        url="https://www.rent.com" + lst["urlPathname"],
        img=imgs[0],
        imgs=imgs,
        nimg=len(imgs),
        body=" ".join(parts)[:1600],
        scrape_status="src",  # gallery+body ship with the row; no page scrape
        src="rent",
    )


def pull_rent():
    seen = {}
    for cname, url in RENT_URLS:
        txt = curl_text(url)
        m = NEXT_DATA_RE.search(txt or "")
        if not m:
            print(f"[rent/{cname}] no __NEXT_DATA__ (blocked or layout change)")
            continue
        d = json.loads(m.group(1))
        items = d["props"]["pageProps"]["pageData"]["location"]["listingSearch"][
            "listings"
        ]
        n_new = 0
        for raw in items:
            r = parse_rent_listing(raw)
            if r is None:
                continue
            if r["pid"] not in seen:
                seen[r["pid"]] = r
                n_new += 1
        print(f"[rent/{cname}] pulled {len(items)}; +{n_new} new; unique={len(seen)}")
        time.sleep(1.2)
    return list(seen.values())


EXTRA_SOURCES = [("rent", pull_rent)]


def pull_extra_sources(sources=None):
    """Best-effort rows from every non-Craigslist source; failures only warn."""
    rows = []
    for name, fn in EXTRA_SOURCES if sources is None else sources:
        try:
            got = fn()
            print(f"[{name}] {len(got)} usable rows")
            rows.extend(got)
        except Exception as e:
            print(
                f"warn: {name} pull failed ({e}) — continuing without it",
                file=sys.stderr,
            )
    return rows


DUP_RADIUS_MI = 0.031  # ~50 m
DUP_PRICE_FRAC = 0.03


def dedupe_cross_source(cl_rows, extra_rows):
    """Same unit on Craigslist AND an extra source -> keep the Craigslist row."""
    anchors = [c for c in cl_rows if c.get("lat") and c.get("lon") and c.get("price")]
    out, n_dropped = list(cl_rows), 0
    for r in extra_rows:
        if not (r.get("lat") and r.get("lon") and r.get("price")):
            out.append(r)
            continue
        dup = any(
            haversine_mi(r["lat"], r["lon"], c["lat"], c["lon"]) <= DUP_RADIUS_MI
            and abs(r["price"] - c["price"])
            <= DUP_PRICE_FRAC * max(r["price"], c["price"])
            for c in anchors
        )
        if dup:
            n_dropped += 1
        else:
            out.append(r)
    if n_dropped:
        print(f"cross-source dedupe: dropped {n_dropped} rows already on Craigslist")
    return out


# --------------------------------------------------------------------------- #
# priors + commute model
# --------------------------------------------------------------------------- #
def priors(hood):
    h = (hood or "").lower().strip()
    for kw, p in RULES:
        if kw in h:
            return p
    return DEFAULT_PRIOR


def transit_estimate_min(lat, lon, anchor):
    """Distance-based transit estimate to an anchor: ~12 mph effective subway
    speed door-to-door plus a 12-min fixed overhead (walk + wait)."""
    mi = haversine_mi(lat, lon, anchor[2], anchor[3])
    return round(mi * 5 + 12)


def anchor_a_min(r):
    """Transit minutes to ANCHOR_A: hood prior if known, else distance model."""
    reg, tmin, *_ = priors(r["hood"])
    if (reg, tmin) != (DEFAULT_PRIOR[0], DEFAULT_PRIOR[1]) or r["lat"] is None:
        return tmin
    return transit_estimate_min(r["lat"], r["lon"], ANCHOR_A)


def anchor_b_min(r):
    """Transit minutes to ANCHOR_B (distance model; None when no ANCHOR_B)."""
    if ANCHOR_B is None or r["lat"] is None:
        return None
    return transit_estimate_min(r["lat"], r["lon"], ANCHOR_B)


def cscore(m):  # minutes -> 0..10
    if m is None:
        return 5.0
    span = (COMMUTE_ZERO - COMMUTE_PERFECT) / 10.0
    return max(0.0, min(10.0, 10 - max(0, m - COMMUTE_PERFECT) / span))


def dual_commute(a_min, b_min):
    if b_min is None:
        return round(cscore(a_min), 1)
    worst = max(a_min, b_min)
    avg = (a_min + b_min) / 2
    return round(0.65 * cscore(worst) + 0.35 * cscore(avg), 1)


def price_fit(r):
    p = r["price"]
    if r["bucket"] == "apt":
        if p <= APT_TARGET:
            return 5.0
        return max(0, 5.0 - (p - APT_TARGET) / max(1, APT_MAX - APT_TARGET) * 3.0)
    if p <= ROOM_TARGET:
        return 5.0
    return max(0, 5.0 - (p - ROOM_TARGET) / max(1, ROOM_MAX - ROOM_TARGET) * 3.0)


def seed_fit(r):
    soft = r["quiet"] if r["bucket"] == "apt" else r["social"]
    return round(
        0.25 * r["nice"]
        + 0.15 * r["nature"]
        + 0.18 * soft
        + 0.20 * price_fit(r)
        + 0.22 * (dual_commute(r["min_a"], r["min_b"]) / 2),
        3,
    )


def norm_hood(h):
    return re.sub(r"\s+", " ", (h or "").lower().strip())


def select_shortlist(rows):
    """Filter to zone/budget, attach priors + seed fit, select ~55 diverse."""
    for r in rows:
        if r["lat"] and r["lon"]:
            r["mi_to_anchor"] = round(
                haversine_mi(r["lat"], r["lon"], ANCHOR_A[2], ANCHOR_A[3]), 1
            )
        else:
            r["mi_to_anchor"] = None
    keep = []
    for r in rows:
        if r["mi_to_anchor"] is None or r["mi_to_anchor"] > MAX_MILES_FROM_ANCHOR:
            continue
        if r["bucket"] == "apt" and not (APT_MIN <= r["price"] <= APT_MAX + 50):
            continue
        if r["bucket"] == "room" and not (ROOM_MIN <= r["price"] <= ROOM_MAX + 50):
            continue
        if r["nimg"] == 0:
            continue
        keep.append(r)

    for r in keep:
        reg, _tmin, nat, quiet, nice, soc = priors(r["hood"])
        r["region"] = reg
        r["nature"], r["quiet"], r["nice"], r["social"] = nat, quiet, nice, soc
        r["min_a"] = anchor_a_min(r)
        r["min_b"] = anchor_b_min(r)
        r["drive_min"] = r["min_a"]  # legacy field name; = anchor A transit min
        r["fit"] = seed_fit(r)

    pool = [r for r in keep if cscore(r["min_a"]) > 0]
    pool.sort(key=lambda r: -r["fit"])

    per_hood_cap = 3
    sel, by_region, by_hood, by_bucket = (
        [],
        collections.Counter(),
        collections.Counter(),
        collections.Counter(),
    )
    for r in pool:
        reg, nh = r["region"], norm_hood(r["hood"])
        if by_region[reg] >= REGION_TARGET.get(reg, 3):
            continue
        if by_hood[nh] >= per_hood_cap:
            continue
        tot = len(sel)
        if tot >= 8:
            frac_apt = by_bucket["apt"] / tot
            if r["bucket"] == "apt" and frac_apt > 0.66:
                continue
            if r["bucket"] == "room" and (1 - frac_apt) > 0.55:
                continue
        sel.append(r)
        by_region[reg] += 1
        by_hood[nh] += 1
        by_bucket[r["bucket"]] += 1
        if len(sel) >= SHORTLIST_SIZE:
            break
    return keep, sel


# --------------------------------------------------------------------------- #
# gyms — walk distance to the nearest OSM gym feeds the fit (WEIGHTS["gym"]).
# --------------------------------------------------------------------------- #
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
GYM_QUERY = (
    "[out:json][timeout:45];("
    f'node["leisure"="fitness_centre"]{GYM_BBOX};'
    f'way["leisure"="fitness_centre"]{GYM_BBOX};'
    f'node["leisure"="sports_centre"]["sport"~"climbing|fitness"]{GYM_BBOX};'
    f'way["leisure"="sports_centre"]["sport"~"climbing|fitness"]{GYM_BBOX};'
    ");out center;"
)


def _overpass_gyms():
    d, last_err = None, None
    for url in OVERPASS_MIRRORS:
        out = subprocess.run(
            [
                "curl",
                "-sS",
                "--max-time",
                "60",
                url,
                "--data-urlencode",
                "data=" + GYM_QUERY,
            ],
            capture_output=True,
            text=True,
            timeout=70,
        )
        try:
            d = json.loads(out.stdout)
            break
        except Exception as e:  # rate-limited / HTML error page — try next mirror
            last_err = e
            print(f"warn: overpass mirror {url} unusable ({e})", file=sys.stderr)
    if d is None:
        raise RuntimeError(f"all overpass mirrors failed: {last_err}")
    pts = []
    for e in d.get("elements", []):
        lat = e.get("lat") or e.get("center", {}).get("lat")
        lon = e.get("lon") or e.get("center", {}).get("lon")
        if lat and lon:
            pts.append([round(lat, 5), round(lon, 5)])
    return pts


def fetch_gyms():
    """Gym coords for the zone. Overpass first (refreshing the committed
    snapshot); fall back to the snapshot, then [] (gym scoring goes neutral)."""
    try:
        pts = _overpass_gyms()
        if len(pts) >= 100:
            json.dump(pts, open(GYMS_JSON, "w"))
            return pts
        print(
            f"warn: overpass returned only {len(pts)} gyms — using snapshot",
            file=sys.stderr,
        )
    except Exception as e:
        print(
            f"warn: overpass gyms fetch failed ({e}) — using snapshot", file=sys.stderr
        )
    try:
        return json.load(open(GYMS_JSON))
    except Exception:
        print("warn: no gyms snapshot — gym scoring neutral this run", file=sys.stderr)
        return []


def nearest_gym_min(lat, lon, gyms):
    if not gyms or lat is None or lon is None:
        return None
    best = min(haversine_mi(lat, lon, g[0], g[1]) for g in gyms)
    return round(best * WALK_FACTOR)


def gym_score(gym_min):
    """10 within a ~4-min walk, 0 at 24+; unknown -> neutral 5 (never punitive)."""
    if gym_min is None:
        return 5.0
    return max(0.0, min(10.0, 10 - max(0, gym_min - 4) / 2.0))


def scrape_page(r):
    """Fetch a listing page; extract full photo gallery + posting body text."""
    try:
        out = subprocess.run(
            ["curl", "-sSL", "--max-time", "25", "-A", UA, r["url"]],
            capture_output=True,
            text=True,
            timeout=30,
        )
        html = out.stdout
    except Exception as e:
        return r["id"], {"imgs": [], "body": "", "status": f"error:{e}"}
    if not html:
        return r["id"], {"imgs": [], "body": "", "status": "empty"}
    dead = bool(DEAD_RE.search(html))
    seen, imgs = set(), []
    for u in IMG_RE.findall(html):
        if u not in seen:
            seen.add(u)
            imgs.append(u)
    body = ""
    m = BODY_RE.search(html)
    if m:
        body = TAG_RE.sub(" ", m.group(1))
        body = re.sub(r"\s+", " ", body).strip()[:1600]
    return r["id"], {
        "imgs": imgs[:24],
        "body": body,
        "status": "dead" if dead else ("ok" if imgs else "noimg"),
    }


def do_pull():
    print("== PULL ==")
    cl_rows = pull_raw()
    if len(cl_rows) < MIN_KEPT:
        sys.exit(
            f"FATAL: only {len(cl_rows)} raw listings pulled from Craigslist "
            f"(expected many). The IP was likely blocked/rate-limited. "
            f"Refusing to build a degraded page."
        )
    extra = pull_extra_sources()
    rows = dedupe_cross_source(cl_rows, extra)
    keep, sel = select_shortlist(rows)
    if len(sel) < MIN_KEPT:
        sys.exit(
            f"FATAL: only {len(sel)} in-zone candidates after filtering "
            f"(< {MIN_KEPT}). Aborting rather than shipping a thin map."
        )
    for i, r in enumerate(sel):
        r["id"] = f"L{i + 1:02d}"
    to_scrape = [r for r in sel if r.get("src", "cl") == "cl"]
    print(f"scraping {len(to_scrape)} craigslist listing pages (photos + body)...")
    with ThreadPoolExecutor(max_workers=8) as ex:
        scraped = dict(ex.map(scrape_page, to_scrape))
    n_dead = n_body = 0
    for r in sel:
        if r.get("src", "cl") != "cl":
            if r.get("body"):
                n_body += 1
            continue  # non-CL rows arrive with full gallery + body
        info = scraped.get(r["id"], {})
        gallery = info.get("imgs") or ([r["img"]] if r["img"] else [])
        r["imgs"] = gallery
        r["img"] = gallery[0] if gallery else r["img"]
        r["nimg"] = len(gallery)
        r["body"] = info.get("body", "")
        r["scrape_status"] = info.get("status", "?")
        if info.get("status") == "dead":
            n_dead += 1
        if r["body"]:
            n_body += 1
    gyms = fetch_gyms()
    for r in sel:
        r["gym_min"] = nearest_gym_min(r["lat"], r["lon"], gyms)
    with_gym = [r["gym_min"] for r in sel if r.get("gym_min") is not None]
    print(
        f"gyms: {len(gyms)} known; median walk "
        f"{int(statistics.median(with_gym)) if with_gym else '?'} min"
    )
    json.dump(sel, open(SHORTLIST, "w"), indent=1)
    json.dump(
        {
            "n_raw": len(cl_rows),
            "n_extra_raw": len(extra),
            "n_extra_kept": len(rows) - len(cl_rows),
            "n_kept": len(keep),
            "n_shortlist": len(sel),
            "pulled": datetime.date.today().isoformat(),
        },
        open(PULL_STATS, "w"),
    )
    print(
        f"wrote {SHORTLIST}: {len(sel)} candidates "
        f"(raw={len(rows)} kept={len(keep)}) | bodies={n_body} dead={n_dead}"
    )
    print("by region:", dict(collections.Counter(r["region"] for r in sel)))
    print("by bucket:", dict(collections.Counter(r["bucket"] for r in sel)))
    print("by source:", dict(collections.Counter(r.get("src", "cl") for r in sel)))


# --------------------------------------------------------------------------- #
# --build
# --------------------------------------------------------------------------- #
def gs(scores, k, d=5):
    v = scores.get(k)
    return d if v is None else v


SRC_LABELS = {"cl": "Craigslist (live API)", "rent": "Rent.com"}

SEARCHLINKS = [
    {
        "group": "NYC-wide",
        "links": [
            {
                "label": f"StreetEasy · rentals ≤${APT_MAX:,}",
                "url": f"https://streeteasy.com/for-rent/nyc/price:-{APT_MAX}",
            },
            {
                "label": f"Zillow · NYC rentals ≤${APT_MAX:,}",
                "url": "https://www.zillow.com/new-york-ny/rentals/",
            },
            {
                "label": f"Apartments.com · NYC ≤${APT_MAX:,}",
                "url": f"https://www.apartments.com/new-york-ny/under-{APT_MAX}/",
            },
            {
                "label": "HotPads map",
                "url": f"https://hotpads.com/new-york-ny/apartments-for-rent?price=0,{APT_MAX}",
            },
        ],
    },
    {
        "group": "Craigslist by borough",
        "links": [
            {
                "label": "Manhattan · apts",
                "url": f"https://newyork.craigslist.org/search/mnh/apa?min_price={APT_MIN}&max_price={APT_MAX}&availabilityMode=0&sort=date",
            },
            {
                "label": "Brooklyn · apts",
                "url": f"https://newyork.craigslist.org/search/brk/apa?min_price={APT_MIN}&max_price={APT_MAX}&availabilityMode=0&sort=date",
            },
            {
                "label": "Queens · apts",
                "url": f"https://newyork.craigslist.org/search/que/apa?min_price={APT_MIN}&max_price={APT_MAX}&availabilityMode=0&sort=date",
            },
            {
                "label": "All NYC · rooms/shares",
                "url": f"https://newyork.craigslist.org/search/roo?max_price={ROOM_MAX}&availabilityMode=0&sort=date",
            },
        ],
    },
    {
        "group": "Rooms & housemates",
        "links": [
            {
                "label": "SpareRoom NYC",
                "url": "https://www.spareroom.com/roommate/new_york",
            },
            {
                "label": "Listings Project",
                "url": "https://www.listingsproject.com/",
            },
            {
                "label": f"Zumper · NYC ≤${APT_MAX:,}",
                "url": f"https://www.zumper.com/apartments-for-rent/new-york-ny?max-price={APT_MAX}",
            },
            {
                "label": "PadMapper (map, all sources)",
                "url": f"https://www.padmapper.com/apartments/new-york-ny?maxPrice={APT_MAX}",
            },
        ],
    },
]


def build_neighborhoods(listings):
    """Deterministic neighborhood cards: group shown listings by hood, average
    the LLM component scores. Keeps groups with >=2 listings, top ~10 by fit."""
    groups = collections.defaultdict(list)
    for x in listings:
        groups[norm_hood(x["hood"])].append(x)
    cards = []
    for nh, xs in groups.items():
        if len(xs) < 2 or not nh:
            continue

        def avg(k):
            vals = [gs(x["scores"], k) for x in xs]
            return round(sum(vals) / len(vals), 1)

        name = max((x["hood"] for x in xs), key=lambda h: len(h or ""))
        cards.append(
            {
                "name": name,
                "region": xs[0]["region"],
                "n": len(xs),
                "avg_fit": round(sum(x["fit"] for x in xs) / len(xs), 1),
                "min_a": round(sum(x["min_a"] for x in xs) / len(xs)),
                "scores": {k: avg(k) for k in ["nature", "quiet", "nice", "social"]},
            }
        )
    cards.sort(key=lambda c: -c["avg_fit"])
    return cards[:10]


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.-]?)?\(?([2-9]\d{2})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})(?!\d)"
)


def extract_contact(body):
    """Pull a poster-supplied email / phone out of the listing body, if present."""
    if not body:
        return None, None
    email = None
    for m in EMAIL_RE.findall(body):
        low = m.lower()
        if any(
            bad in low
            for bad in (
                "craigslist.org",
                "reply.craigslist",
                "example.",
                "@2x",
                ".png",
                ".jpg",
            )
        ):
            continue
        email = m
        break
    phone = None
    pm = PHONE_RE.search(body)
    if pm:
        phone = f"({pm.group(1)}) {pm.group(2)}-{pm.group(3)}"
    return email, phone


def load_data_js():
    """Parse the existing data.js back into a dict (None if absent/unparseable)."""
    if not os.path.exists(DATA_JS):
        return None
    try:
        txt = open(DATA_JS).read()
        return json.loads(
            txt[txt.index("=") + 1 : txt.rstrip().rstrip(";").rindex("}") + 1]
        )
    except Exception as e:
        print(f"warn: could not parse existing data.js ({e})", file=sys.stderr)
        return None


def write_data_js(data):
    with open(DATA_JS, "w") as f:
        f.write(
            "window.HOUSES_DATA = " + json.dumps(data, separators=(",", ":")) + ";\n"
        )


def check_live(url):
    """True = alive, False = positive dead signal, None = unknown (treated alive)."""
    try:
        out = subprocess.run(
            ["curl", "-sSL", "--max-time", "20", "-A", UA, "-w", "\n%{http_code}", url],
            capture_output=True,
            text=True,
            timeout=25,
        )
        body, _, code = out.stdout.rpartition("\n")
        if code in ("404", "410"):
            return False
        if not body:
            return None
        return not bool(DEAD_RE.search(body))
    except Exception:
        return None


def do_sweep():
    """Keyless freshness pass: re-check every shown listing's page and prune
    the dead ones. No re-rating."""
    print("== SWEEP ==")
    data = load_data_js()
    if data is None:
        sys.exit("FATAL: no parseable data.js to sweep.")
    listings = data["listings"]
    with ThreadPoolExecutor(max_workers=4) as ex:
        alive = dict(ex.map(lambda x: (x["url"], check_live(x["url"])), listings))

    kept, pruned = [], []
    for x in listings:
        if alive.get(x["url"]) is False:
            pruned.append(x["id"])
            continue
        kept.append(x)

    if len(kept) < 10:
        sys.exit(
            f"FATAL: sweep would leave only {len(kept)} listings — mass-death is "
            f"more likely a scrape problem than reality. Not writing."
        )
    if not pruned:
        print(f"sweep: all {len(kept)} listings still live; no changes.")
        return

    data["listings"] = kept
    data["neighborhoods"] = build_neighborhoods(kept)
    data["meta"]["n_shown"] = len(kept)
    data["meta"]["swept"] = datetime.date.today().isoformat()
    write_data_js(data)
    print(f"sweep: pruned {len(pruned)} dead {pruned}, kept {len(kept)}.")


def renter_fit(scores, bucket, dual_c, gym):
    """Weighted sum (soft = quiet for apt, social for room). Loved bonus, cap,
    and rounding stay at the call site. Mirrored by fitmath.js in the viewer."""
    soft = gs(scores, "quiet") if bucket == "apt" else gs(scores, "social")
    return (
        WEIGHTS["gym"] * gym
        + WEIGHTS["nice"] * gs(scores, "nice")
        + WEIGHTS["nature"] * gs(scores, "nature")
        + WEIGHTS["soft"] * soft
        + WEIGHTS["value"] * gs(scores, "value")
        + WEIGHTS["commute"] * dual_c
        + WEIGHTS["aesthetic"] * gs(scores, "aesthetic")
    )


def do_build():
    print("== BUILD ==")
    if not os.path.exists(SHORTLIST):
        sys.exit(f"FATAL: {SHORTLIST} missing — run --pull first.")
    if not os.path.exists(RATINGS):
        sys.exit(f"FATAL: {RATINGS} missing — rate.py must write it before --build.")
    rows_all = json.load(open(SHORTLIST))
    sel = {r["id"]: r for r in rows_all}
    rated = json.load(open(RATINGS))
    ratings = {rr["id"]: rr for rr in rated}
    print(f"shortlist={len(sel)} rated={len(ratings)}")

    listings = []
    for lid, r in sel.items():
        rt = ratings.get(lid)
        if rt is None:
            continue  # not rated -> skip
        if rt.get("live") is False or rt.get("commercial") is True:
            continue
        if (rt.get("fit") or 0) <= 2:
            continue
        src = rt.get("scores", rt)
        scores = {
            k: src.get(k)
            for k in [
                "nature",
                "quiet",
                "nice",
                "social",
                "value",
                "commute",
                "aesthetic",
            ]
        }
        rationale = rt.get("rationale") or rt.get("why") or ""
        gallery = r.get("imgs") or ([r["img"]] if r.get("img") else [])
        email, phone = extract_contact(r.get("body", ""))
        listings.append(
            {
                "id": lid,
                "price": r["price"],
                "pdisp": r.get("pdisp") or f"${r['price']:,}",
                "beds": r.get("beds"),
                "bucket": r["bucket"],
                "hood": r["hood"],
                "region": r["region"],
                "lat": r["lat"],
                "lon": r["lon"],
                "url": r["url"],
                "img": gallery[0] if gallery else r.get("img"),
                "imgs": gallery,
                "nimg": len(gallery),
                "min_a": r["min_a"],
                "min_b": r.get("min_b"),
                "drive_min": r["min_a"],
                "gym_min": r.get("gym_min"),
                "title": r.get("title", ""),
                "src": r.get("src", "cl"),
                "fit": rt.get("fit"),
                "scores": scores,
                "rationale": rationale,
                "contact_email": email,
                "contact_phone": phone,
            }
        )

    # commute + final fit (blends LLM component scores w/ the commute model)
    for x in listings:
        x["dual_commute"] = dual_commute(x["min_a"], x.get("min_b"))
        x["loved"] = any(k in (x["hood"] or "").lower() for k in LOVED)
        s = x["scores"]
        gymsc = gym_score(x.get("gym_min"))
        fit = renter_fit(s, x["bucket"], x["dual_commute"], gymsc)
        if x["loved"]:
            fit += 0.8
        x["fit"] = round(min(10.0, fit), 1)
        s["commute"] = round(x["dual_commute"], 1)
        s["gym"] = round(gymsc, 1)

    # dedupe by URL (keep higher fit)
    by_url = {}
    for x in sorted(listings, key=lambda x: -(x["fit"] or 0)):
        by_url.setdefault(x["url"], x)
    listings = sorted(by_url.values(), key=lambda x: -(x["fit"] or 0))

    # first_seen: stamp when each URL first appeared so the UI can flag
    # same-day arrivals. Carried forward by URL across builds.
    prev_seen = {
        x["url"]: x.get("first_seen")
        for x in (load_data_js() or {}).get("listings", [])
    }
    today = datetime.date.today().isoformat()
    for x in listings:
        x["first_seen"] = prev_seen.get(x["url"]) if x["url"] in prev_seen else today

    if len(listings) < MIN_SHOWN:
        sys.exit(
            f"FATAL: only {len(listings)} listings survived rating/filtering "
            f"(< {MIN_SHOWN}). Not overwriting data.js with a thin page."
        )

    # top picks: genuinely good fit, keep region variety
    seen_reg = {}
    for x in listings:
        reg = x["region"]
        if x["fit"] >= 7 and seen_reg.get(reg, 0) < 2 and sum(seen_reg.values()) < 6:
            x["pick"] = True
            seen_reg[reg] = seen_reg.get(reg, 0) + 1
        else:
            x["pick"] = False

    neighborhoods = build_neighborhoods(listings)
    stats = {}
    if os.path.exists(PULL_STATS):
        stats = json.load(open(PULL_STATS))
    src_counts = dict(collections.Counter(x.get("src", "cl") for x in listings))
    src_order = ["cl", "rent"] + sorted(set(src_counts) - {"cl", "rent"})
    src_str = " + ".join(SRC_LABELS.get(k, k) for k in src_order if k in src_counts)
    anchors_meta = [
        {
            "label": ANCHOR_A[0],
            "short": ANCHOR_A[1],
            "lat": ANCHOR_A[2],
            "lon": ANCHOR_A[3],
        }
    ]
    if ANCHOR_B is not None:
        anchors_meta.append(
            {
                "label": ANCHOR_B[0],
                "short": ANCHOR_B[1],
                "lat": ANCHOR_B[2],
                "lon": ANCHOR_B[3],
            }
        )
    meta = {
        "generated": today,
        "n_scouted": stats.get("n_kept", len(sel)),
        "n_shortlist": stats.get("n_shortlist", len(sel)),
        "n_shown": len(listings),
        "sources": src_counts,
        "source": f"{src_str}, refreshed {today}",
        "anchors": anchors_meta,
        "price_min": min(x["price"] for x in listings),
        "price_max": max(x["price"] for x in listings),
        "price_med": int(statistics.median(x["price"] for x in listings)),
        "fit_weights": {"renter": WEIGHTS},
    }
    data = {
        "meta": meta,
        "listings": listings,
        "neighborhoods": neighborhoods,
        "searchlinks": SEARCHLINKS,
    }
    write_data_js(data)
    picks = [x for x in listings if x["pick"]]
    print(
        f"wrote {DATA_JS}: shown={len(listings)} picks={len(picks)} "
        f"neighborhoods={len(neighborhoods)}"
    )
    print("top 6:", [(x["id"], x["hood"], x["fit"]) for x in listings[:6]])


def main():
    ap = argparse.ArgumentParser(description="Daily refresh for the NYC rental scout")
    ap.add_argument("--pull", action="store_true", help="pull + shortlist + scrape")
    ap.add_argument("--build", action="store_true", help="merge ratings -> data.js")
    ap.add_argument(
        "--sweep", action="store_true", help="prune dead listings (keyless)"
    )
    args = ap.parse_args()
    if args.pull:
        do_pull()
    elif args.build:
        do_build()
    elif args.sweep:
        do_sweep()
    else:
        ap.error("specify --pull, --build, or --sweep")


if __name__ == "__main__":
    main()
