"""
BMS Ticket Checker — CI/Headless mode for GitHub Actions.
Runs once, checks all configured watches, emails on changes.
State is persisted via a JSON artifact.

Configure via environment variables or edit the CONFIG below.
"""

import os
import re
import sys
import json
import time
from html import escape
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urlparse
import requests

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these or set via env vars
# ──────────────────────────────────────────────────────────────────────
CONFIG = {
    "url": os.getenv(
        "BMS_URL",
        "https://in.bookmyshow.com/movies/chennai/dhurandhar-the-revenge/buytickets/ET00478890"
    ),
    "dates": os.getenv("BMS_DATES", ""),          # comma-separated YYYYMMDD, empty = from URL
    "theatre": os.getenv("BMS_THEATRE", ""),       # substring filter, empty = all
    "time_period": os.getenv("BMS_TIME", ""),      # e.g. "evening,night", empty = all
    "screen": os.getenv("BMS_SCREEN", ""),         # e.g. "PCX,IMAX", empty = all
}

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "aviiciii@resend.dev")

STATE_FILE = "bms_state.json"

# ──────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────
AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT",    "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST","🟠"),
    "3": ("AVAILABLE",   "🟢"),
}

DATE_STYLE_MAP = {
    "date-selected": "BOOKABLE",
    "date-disabled": "NOT_OPEN",
    "date-default":  "AVAILABLE",
}

# How "open" each date status is, for reconciling the same date seen twice.
DATE_OPENNESS = {
    "NOT_LISTED": 0,
    "UNKNOWN":    1,
    "NOT_OPEN":   2,
    "AVAILABLE":  3,
    "BOOKABLE":   4,
}

TIME_PERIODS = {
    "morning":   (600, 1200),
    "afternoon": (1200, 1600),
    "evening":   (1600, 1900),
    "night":     (1900, 2400),
}

REGION_MAP = {
    "chennai":    ("CHEN",   "chennai",    "13.056", "80.206", "tf3"),
    "mumbai":     ("MUMBAI", "mumbai",     "19.076", "72.878", "te7"),
    "delhi-ncr":  ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "delhi":      ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "bengaluru":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "bangalore":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "hyderabad":  ("HYD",    "hyderabad",  "17.385", "78.487", "tep"),
    "kolkata":    ("KOLK",   "kolkata",    "22.573", "88.364", "tun"),
    "pune":       ("PUNE",   "pune",       "18.520", "73.856", "te2"),
    "kochi":      ("KOCH",   "kochi",      "9.932",  "76.267", "t9z"),
}


# ─────────────────────────────────────���────────────────────────────────
# DATA
# ──────────────────────────────────────────────────────────────────────
@dataclass
class CatInfo:
    name: str
    price: str
    status: str

@dataclass
class ShowInfo:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    screen_name: str = ""
    categories: list[CatInfo] = field(default_factory=list)

    def screen_haystack(self):
        """Lowercased text to match BMS_SCREEN against.

        BMS puts the format label in different places depending on the
        venue, so match against every screen-ish field we captured.
        """
        return " ".join(
            p for p in (self.screen_attr, self.screen_name) if p
        ).lower()

@dataclass
class DateInfo:
    date_code: str
    status: str


# ──────────────────────────────────────────────────────────────────────
# URL PARSER + REGION RESOLVER
# ──────────────────────────────────────────────────────────────────────
def parse_bms_url(url):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    result = {"event_code": None, "date_code": None, "region_slug": None}
    for p in parts:
        if re.match(r"^ET\d{8,}$", p):
            result["event_code"] = p
        elif re.match(r"^\d{8}$", p):
            result["date_code"] = p
    if "movies" in parts:
        idx = parts.index("movies")
        if idx + 1 < len(parts):
            result["region_slug"] = parts[idx + 1]
    return result


def resolve_region(slug):
    key = (slug or "").lower().strip()
    if key in REGION_MAP:
        return REGION_MAP[key]
    return (key.upper()[:6], key, "0", "0", "")


# ──────────────────────────────────────────────────────────────────────
# BMS API
# ──────────────────────────────────────────────────────────────────────
API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)


def fetch_bms(event_code, date_code, region_code, region_slug,
              lat, lon, geohash):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": (
            f"https://in.bookmyshow.com/movies/"
            f"{region_slug}/buytickets/{event_code}/"
        ),
        "sec-ch-ua": '"Chromium";v="145", "Not:A-Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-longitude": lon,
        "x-location-selection": "manual",
        "x-lsid": "",
    }
    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "memberId": "", "lsId": "", "subCode": "",
        "lat": lat, "lon": lon,
    }
    try:
        resp = requests.get(API_URL, headers=headers,
                            params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        print(f"  HTTP {resp.status_code}")
    except requests.RequestException as e:
        print(f"  Request failed: {e}")
    return None


# ──────────────────────────────────────────────────────────────────────
# PARSERS
# ──────────────────────────────────────────────────────────────────────
def parse_movie_info(data):
    info = {"name": "Unknown Movie", "language": ""}
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") == "horizontal-text-list":
            for item in w.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for c in row.get("components", []):
                        if "•" in c.get("text", ""):
                            info["language"] = c["text"].strip()
    bs = data.get("data", {}).get("bottomSheetData", {})
    for w in bs.get("format-selector", {}).get("widgets", []):
        if w.get("type") == "vertical-text-list":
            for d in w.get("data", []):
                if d.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = d.get("text", info["name"])
    return info


def parse_dates(data):
    dates = []
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") != "horizontal-block-list":
            continue
        for item in w.get("data", []):
            texts = item.get("data", [])
            if len(texts) >= 3:
                style = item.get("styleId", "")
                dates.append(DateInfo(
                    date_code=item.get("id", ""),
                    status=DATE_STYLE_MAP.get(style, "UNKNOWN"),
                ))
    return dates


def parse_shows(data):
    shows = []
    for w in data.get("data", {}).get("showtimeWidgets", []):
        if w.get("type") != "groupList":
            continue
        for g in w.get("data", []):
            if g.get("type") != "venueGroup":
                continue
            for card in g.get("data", []):
                if card.get("type") != "venue-card":
                    continue
                addl = card.get("additionalData", {})
                vname = addl.get("venueName", "Unknown")
                vcode = addl.get("venueCode", "")

                for st in card.get("showtimes", []):
                    sa = st.get("additionalData", {})
                    date_code = str(
                        sa.get("showDateCode", "")
                        or sa.get("dateCode", "")
                    ).strip()
                    if not date_code and re.match(
                            r"^\d{8}", sa.get("cutOffDateTime", "")):
                        date_code = sa["cutOffDateTime"][:8]

                    show = ShowInfo(
                        venue_code=vcode,
                        venue_name=vname,
                        session_id=sa.get("sessionId", ""),
                        date_code=date_code,
                        time=st.get("title", ""),
                        time_code=sa.get("showTimeCode", ""),
                        screen_attr=(st.get("screenAttr", "")
                                     or sa.get("attributes", "")),
                        screen_name=(sa.get("screenName", "")
                                     or sa.get("screen", "")),
                    )
                    for cat in sa.get("categories", []):
                        ca = str(cat.get("availStatus", ""))
                        lbl, _ = AVAIL_STATUS_MAP.get(ca, ("UNKNOWN", ""))
                        show.categories.append(CatInfo(
                            name=cat.get("priceDesc", ""),
                            price=cat.get("curPrice", "0"),
                            status=ca,
                        ))
                    shows.append(show)
    return shows


# ──────────────────────────────────────────────────────────────────────
# FILTERING
# ──────────────────────────────────────────────────────────────────────
def parse_watches():
    """Return the list of independent watches to check.

    BMS_WATCHES is a JSON array, one object per watch, so that unrelated
    targets ("PCX on Aug 1", "Dolby on Jul 28") stay separate instead of
    being combined into one filter set that would match their cross-product.
    Falls back to the flat BMS_* vars when unset.

    Each watch may carry its own "url" — a premium format like Dolby Cinema
    2D or IMAX is a SEPARATE BMS event code, not a screen inside the standard
    listing, so filtering the standard event can never find it. A watch with
    no url uses the global BMS_URL.
    """
    raw = os.getenv("BMS_WATCHES", "").strip()
    if not raw:
        entries = [{
            "name": "default",
            "dates": CONFIG["dates"],
            "theatre": CONFIG["theatre"],
            "screen": CONFIG["screen"],
            "time": CONFIG["time_period"],
        }]
    else:
        entries = json.loads(raw)

    watches = []
    for i, w in enumerate(entries):
        url = str(w.get("url") or CONFIG["url"])
        parsed = parse_bms_url(url)
        if not parsed["event_code"] or not parsed["region_slug"]:
            raise ValueError(
                f"watch {w.get('name') or i + 1}: cannot extract event/region "
                f"from url {url!r}")
        watches.append({
            "name": str(w.get("name") or f"watch{i + 1}"),
            "dates": str(w.get("dates", "")),
            "theatre": str(w.get("theatre", "")),
            "screen": str(w.get("screen", "")),
            "time": str(w.get("time", "")),
            "url": url,
            "event_code": parsed["event_code"],
            "region_slug": parsed["region_slug"],
            "region": resolve_region(parsed["region_slug"]),
        })
    if not watches:
        raise ValueError("BMS_WATCHES is an empty list")

    names = [w["name"] for w in watches]
    if len(set(names)) != len(names):
        # State is keyed by name, so duplicates would silently share a slice.
        raise ValueError(f"BMS_WATCHES has duplicate names: {names}")
    return watches


def warn_empty_filter(all_shows, watch):
    """Show what BMS actually returned when the filters match nothing.

    Filters matching nothing is expected while a future date is still closed,
    but it is also what a typo in a theatre/screen filter looks like — and that
    failure is silent. Print the real values so the run log can tell them apart.
    """
    print("     ⚠️  matched 0 of "
          f"{len(all_shows)} showtime(s). Values seen in the feed:")

    theatre_kws = [k.strip().lower() for k in watch["theatre"].split(",")
                   if k.strip()]
    in_theatre = [
        s for s in all_shows
        if not theatre_kws
        or any(k in s.venue_name.lower() for k in theatre_kws)
    ]

    venues = sorted({s.venue_name for s in all_shows})
    print(f"     venues ({len(venues)}): {', '.join(venues[:8])}")

    if theatre_kws and not in_theatre:
        print(f"     ❗ theatre={watch['theatre']!r} matched no venue.")
        return

    scope = "matching venues" if theatre_kws else "all venues"
    screens = sorted({s.screen_haystack() for s in in_theatre if
                      s.screen_haystack()})
    print(f"     screens at {scope}: {', '.join(screens) or '(none reported)'}")
    dates = sorted({s.date_code for s in in_theatre if s.date_code})
    print(f"     dates at {scope}: {', '.join(dates)}")

    if watch["screen"] and screens and not any(
        sc.strip().lower() in h
        for sc in watch["screen"].split(",") if sc.strip()
        for h in screens
    ):
        print(f"     ❗ screen={watch['screen']!r} matched none of the "
              f"above. If the target date is open, this is a config error.")


def parse_date_codes(date_codes):
    if not date_codes:
        return set()
    return set(d.strip() for d in date_codes.split(",") if d.strip())


def filter_shows(shows, theatre_filter, time_periods, date_codes,
                 screen_filter=""):
    result = []
    kws = [k.strip().lower() for k in theatre_filter.split(",")
           if k.strip()] if theatre_filter else []
    periods = [p.strip().lower() for p in time_periods.split(",")
               if p.strip()] if time_periods else []
    dates_set = parse_date_codes(date_codes)
    screens = [s.strip().lower() for s in screen_filter.split(",")
               if s.strip()] if screen_filter else []

    for s in shows:
        # Theatre filter
        if kws:
            name_lower = s.venue_name.lower()
            if not any(k in name_lower for k in kws):
                continue

        # Date filter
        if dates_set and s.date_code and s.date_code not in dates_set:
            continue

        # Screen / format filter (e.g. PCX, IMAX)
        if screens:
            haystack = s.screen_haystack()
            if not any(sc in haystack for sc in screens):
                continue

        # Time period filter
        if periods:
            try:
                tc = int(s.time_code)
            except ValueError:
                tc = 0
            matched = False
            for p in periods:
                if p in TIME_PERIODS:
                    lo, hi = TIME_PERIODS[p]
                    if lo <= tc < hi:
                        matched = True
                        break
            if not matched:
                continue

        result.append(s)
    return result


# ──────────────────────────────────────────────────────────────────────
# STATE (for change detection between runs)
# ──────────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def build_state(shows, dates, watched_dates=None):
    """Build a comparable state dict.

    When watched_dates is set, only those dates are tracked — otherwise the
    date strip rolling forward one day would look like a new date opening
    on every single run.
    """
    show_state = {}
    for s in shows:
        for c in s.categories:
            key = f"{s.venue_code}|{s.session_id}|{s.date_code}|{c.name}"
            show_state[key] = {
                "venue": s.venue_name,
                "time": s.time,
                "date": s.date_code,
                "cat": c.name,
                "price": c.price,
                "status": c.status,
            }

    # We fetch more than one view, so the same date can arrive twice with
    # different statuses. Keep the most-open one — a stale or degraded view
    # must never mask a date that has actually opened.
    date_state = {}
    for d in dates:
        if watched_dates and d.date_code not in watched_dates:
            continue
        known = date_state.get(d.date_code)
        if known is None or DATE_OPENNESS.get(d.status, 0) > DATE_OPENNESS.get(
                known, 0):
            date_state[d.date_code] = d.status

    # A watched date BMS hasn't listed yet is tracked explicitly, so that its
    # first appearance registers as a change rather than as a brand-new key.
    for dc in (watched_dates or set()):
        date_state.setdefault(dc, "NOT_LISTED")

    return {"shows": show_state, "dates": date_state}


def _group_showtimes(show_state):
    """Collapse the per-category state into one entry per showtime.

    State is keyed 'venue|session|date|category'; the showtime is the key
    without the category, so a single show with four price categories is one
    showtime, not four separate changes.
    """
    out = {}
    for key, v in show_state.items():
        sid = key.rsplit("|", 1)[0]
        st = out.setdefault(sid, {"venue": v["venue"], "time": v["time"],
                                  "date": v["date"], "cats": []})
        st["cats"].append({"name": v["cat"], "price": v["price"],
                           "status": v["status"]})
    return out


def detect_changes(old_state, new_state):
    """Return structured changes (dicts), one per showtime/date/seat — NOT one
    per category. Kinds: 'date_open', 'new_show', 'back'. Formatting for the
    console and the email is done separately so each stays concise."""
    changes = []

    old_dates = old_state.get("dates", {})
    new_dates = new_state.get("dates", {})
    CLOSED = (None, "NOT_OPEN", "NOT_LISTED", "UNKNOWN")
    for dc, status in new_dates.items():
        if (old_dates.get(dc) in CLOSED
                and status in ("BOOKABLE", "AVAILABLE")):
            changes.append({"kind": "date_open", "date": dc})

    old_shows = old_state.get("shows", {})
    new_shows = new_state.get("shows", {})
    old_sids = {k.rsplit("|", 1)[0] for k in old_shows}

    # One 'new_show' per newly-seen showtime, carrying all its categories.
    for sid, st in _group_showtimes(new_shows).items():
        if sid not in old_sids:
            changes.append({"kind": "new_show", **st})

    # A seat freeing up on a show we already knew — the actionable signal.
    for key, new_s in new_shows.items():
        old_s = old_shows.get(key)
        if old_s and old_s["status"] == "0" and new_s["status"] != "0":
            changes.append({
                "kind": "back", "venue": new_s["venue"], "time": new_s["time"],
                "date": new_s["date"], "cat": new_s["cat"],
                "price": new_s["price"], "status": new_s["status"],
            })

    return changes


def bootstrap_changes(new_state):
    """Concise current-state summary for a watch's first run.

    Reports only what is actionable right now — watched dates that are already
    open, and showtimes that have at least one bookable category. Sold-out
    shows are not announced (a later free-up is caught by detect_changes), so
    setting up a watch on an already-live target notifies without flooding.
    """
    changes = []
    for dc, status in new_state.get("dates", {}).items():
        if status in ("BOOKABLE", "AVAILABLE"):
            changes.append({"kind": "date_open", "date": dc, "bootstrap": True})
    for sid, st in _group_showtimes(new_state.get("shows", {})).items():
        if any(c["status"] != "0" for c in st["cats"]):
            changes.append({"kind": "new_show", "bootstrap": True, **st})
    return changes


def change_line(ch):
    """One-line console/plain-text rendering of a structured change."""
    if ch["kind"] == "date_open":
        verb = "already open" if ch.get("bootstrap") else "OPENED"
        return f"📅 DATE {verb}: {fmt_date(ch['date'])}"
    if ch["kind"] == "back":
        lbl = AVAIL_STATUS_MAP.get(ch["status"], ("?", ""))[0]
        return (f"🟢 SEAT BACK: {ch['cat']} — {ch['time']}, "
                f"{fmt_date(ch['date'])} ({lbl})")
    # new_show
    avail = [c for c in ch["cats"] if c["status"] != "0"]
    sold = len(ch["cats"]) - len(avail)
    verb = "live" if ch.get("bootstrap") else "NEW show"
    parts = ", ".join(f"{c['name']} {AVAIL_STATUS_MAP.get(c['status'],('?',''))[0]}"
                      for c in sorted(avail, key=lambda c: c["status"], reverse=True))
    tail = parts or "no seats yet"
    if sold and parts:
        tail += f" (+{sold} sold out)"
    return f"🎬 {verb}: {ch['time']}, {fmt_date(ch['date'])} — {tail}"


def change_subject(changes):
    """One-line email subject that leads with the most useful change."""
    backs = [c for c in changes if c["kind"] == "back"]
    if backs:
        b = backs[0]
        extra = f" +{len(backs) - 1} more" if len(backs) > 1 else ""
        return f"🟢 {b['cat']} back — {b['time']} {fmt_date(b['date'])}{extra}"
    dates = [c for c in changes if c["kind"] == "date_open"
             and not c.get("bootstrap")]
    if dates:
        return f"📅 {fmt_date(dates[0]['date'])} opened for booking"
    live = [c for c in changes if c["kind"] == "new_show"
            and any(x["status"] != "0" for x in c["cats"])]
    if live:
        s = live[0]
        return f"🎬 New show {s['time']}, {fmt_date(s['date'])}"
    n = len(changes)
    return f"{n} update{'s' if n != 1 else ''}"


# ──────────────────────────────────────────────────────────────────────
# EMAIL NOTIFICATION (Resend)
# ──────────────────────────────────────────────────────────────────────
def _cat_status_label(status):
    return AVAIL_STATUS_MAP.get(status, ("UNKNOWN", ""))[0]


# status -> (label, emoji, text colour, background) for the availability badges.
STATUS_STYLE = {
    "0": ("SOLD OUT",     "🔴", "#6b7280", "#f3f4f6"),
    "1": ("ALMOST FULL",  "🟡", "#92400e", "#fef3c7"),
    "2": ("FILLING FAST", "🟠", "#9a3412", "#ffedd5"),
    "3": ("AVAILABLE",    "🟢", "#166534", "#dcfce7"),
}


def fmt_date(date_code):
    """20260729 -> 'Wed 29 Jul'. Falls back to the raw code if unparseable."""
    try:
        return datetime.strptime(date_code, "%Y%m%d").strftime("%a %d %b")
    except (ValueError, TypeError):
        return date_code


def booking_url(watch, date_code):
    """Direct BMS booking link for one date of a watch's event."""
    base = watch["url"].split("/buytickets/")[0]
    return f"{base}/buytickets/{watch['event_code']}/{date_code}"


def send_email(subject, changes, shows, movie_info, watch):
    api_key = RESEND_API_KEY.strip()
    to = RESEND_TO_EMAIL.strip()
    frm = RESEND_FROM_EMAIL.strip() or "onboarding@resend.dev"

    if not api_key or not to:
        # Hard failure, not a skip. State is already written, so returning
        # quietly would consume the change and never report it again — the
        # alert would be lost precisely when there was something to say.
        # Exiting non-zero keeps the run from committing state, so the next
        # run re-detects and retries.
        print("  ❌ Have changes to report but RESEND_API_KEY or "
              "RESEND_TO_EMAIL is not set. Alert LOST — failing so the "
              "state is not committed and the next run retries.")
        sys.exit(1)

    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    movie_name = movie_info.get("name", "Movie")
    watch_label = watch.get("name", "")

    # ── WHAT CHANGED — the whole point of the email, so it leads. Rendered
    # one line per showtime/date/seat (not per category), actionable first,
    # with sold-out-only new shows collapsed to a single muted line. ───────
    bootstrap = any(c.get("bootstrap") for c in changes)
    banner_title = ("📸 Current status (new watch)" if bootstrap
                    else "⚡ What changed since the last alert")

    def _row(inner):
        return (f'<tr><td style="padding:7px 12px;font-size:14px;'
                f'border-top:1px solid #fde68a;">{inner}</td></tr>')

    if changes:
        ordered = ([c for c in changes if c["kind"] == "back"]
                   + [c for c in changes if c["kind"] == "date_open"]
                   + [c for c in changes if c["kind"] == "new_show"])
        rows, soldout_new = [], 0
        for ch in ordered:
            if ch["kind"] == "date_open":
                verb = "is open" if ch.get("bootstrap") else "opened for booking"
                rows.append(_row(
                    f'📅 <b style="color:#166534;">{escape(fmt_date(ch["date"]))}'
                    f'</b> {verb}'))
            elif ch["kind"] == "back":
                rows.append(_row(
                    f'🟢 <b style="color:#166534;">{escape(ch["cat"])}</b> is '
                    f'back — {escape(ch["time"])}, {escape(fmt_date(ch["date"]))}'))
            else:  # new_show
                avail = sorted((c for c in ch["cats"] if c["status"] != "0"),
                               key=lambda c: c["status"], reverse=True)
                if not avail:
                    soldout_new += 1
                    continue
                sold = len(ch["cats"]) - len(avail)
                seats = " · ".join(
                    f'{STATUS_STYLE.get(c["status"], ("", "⚪"))[1]} '
                    f'{escape(c["name"])}' for c in avail)
                extra = (f' <span style="color:#9ca3af;">(+{sold} sold out)'
                         f'</span>') if sold else ""
                verb = "Live now" if ch.get("bootstrap") else "New show"
                rows.append(_row(
                    f'🎬 {verb} <b>{escape(ch["time"])}, '
                    f'{escape(fmt_date(ch["date"]))}</b> — {seats}{extra}'))
        if soldout_new:
            rows.append(_row(
                f'<span style="color:#9ca3af;">＋ {soldout_new} more showtime'
                f'{"s" if soldout_new != 1 else ""} — no seats yet</span>'))
        change_rows = "".join(rows)
        changes_html = f"""
    <table role="presentation" width="100%" style="border-collapse:collapse;
           background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;
           margin:0 0 22px 0;overflow:hidden;">
        <tr><td style="padding:10px 12px 3px 12px;font-size:12px;
               font-weight:700;letter-spacing:.04em;color:#b45309;
               text-transform:uppercase;">{banner_title}</td></tr>
        {change_rows}
    </table>"""
    else:
        changes_html = ""

    # ── CURRENT SHOWTIMES — grouped by date, each date with a Book link. ───
    by_date = {}
    for s in shows:
        by_date.setdefault(s.date_code, []).append(s)

    # Available first so the bookable options lead; sold-out sinks to the end.
    avail_rank = {"3": 0, "2": 1, "1": 2, "0": 3}

    def badge(cat):
        lbl, emoji, fg, bg = STATUS_STYLE.get(cat.status,
                                              ("UNKNOWN", "⚪", "#374151", "#f3f4f6"))
        return (
            f'<span style="display:inline-block;padding:2px 8px;margin:2px 4px 2px 0;'
            f'font-size:12px;border-radius:12px;color:{fg};background:{bg};'
            f'white-space:nowrap;">{emoji} {escape(cat.name)} '
            f'₹{escape(cat.price)} · {lbl}</span>'
        )

    shows_html = ""
    for dc in sorted(by_date):
        link = booking_url(watch, dc)
        rows = ""
        for s in by_date[dc]:
            fmt = f' <span style="color:#6b7280;">[{escape(s.screen_attr)}]</span>' \
                  if s.screen_attr else ""
            cats = sorted(s.categories, key=lambda c: avail_rank.get(c.status, 9))
            badges = "".join(badge(c) for c in cats)
            # Green accent + flag when something is bookable; muted when not.
            bookable = any(c.status != "0" for c in s.categories)
            accent = "#16a34a" if bookable else "#e5e7eb"
            flag = ('<span style="color:#166534;font-size:11px;font-weight:700;">'
                    '● seats open</span>') if bookable else (
                    '<span style="color:#9ca3af;font-size:11px;font-weight:600;">'
                    '● sold out</span>')
            rows += (
                f'<tr>'
                f'<td style="padding:8px 10px 8px 12px;border-top:1px solid #eee;'
                f'border-left:3px solid {accent};font-size:14px;font-weight:600;'
                f'white-space:nowrap;vertical-align:top;color:#111827;">'
                f'{escape(s.time)}{fmt}<br>{flag}</td>'
                f'<td style="padding:6px 10px;border-top:1px solid #eee;">{badges}</td>'
                f'</tr>'
            )
        shows_html += f"""
    <table role="presentation" width="100%" style="border-collapse:collapse;
           border:1px solid #e5e7eb;border-radius:8px;margin:0 0 16px 0;">
        <tr><td colspan="2" style="padding:10px;background:#f9fafb;
               border-bottom:1px solid #e5e7eb;border-radius:8px 8px 0 0;">
            <span style="font-size:15px;font-weight:700;color:#111827;">
                📅 {escape(fmt_date(dc))}</span>
            <a href="{escape(link)}" style="float:right;background:#2563eb;
               color:#fff;text-decoration:none;font-size:13px;font-weight:600;
               padding:6px 14px;border-radius:6px;">Book on BookMyShow →</a>
        </td></tr>
        {rows}
    </table>"""

    if not shows_html:
        shows_html = ('<p style="font-size:14px;color:#6b7280;">No matching '
                      'showtimes are listed yet — you\'ll get another alert when '
                      'they appear.</p>')

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:20px;font-family:-apple-system,Segoe UI,Arial,sans-serif;
             font-size:14px;color:#111827;background:#f3f4f6;">
  <table role="presentation" width="100%" style="max-width:600px;margin:0 auto;
         background:#fff;border-radius:10px;padding:22px;">
    <tr><td>
      <div style="font-size:19px;font-weight:800;color:#111827;">
        🎬 {escape(movie_name)}</div>
      <div style="font-size:13px;color:#6b7280;margin:2px 0 18px 0;">
        Watch: <b>{escape(watch_label)}</b> · {escape(now_str)}</div>
      {changes_html}
      <div style="font-size:12px;font-weight:700;letter-spacing:.04em;
           color:#374151;text-transform:uppercase;margin:0 0 10px 0;">
        🎟️ Current showtimes</div>
      {shows_html}
      <div style="font-size:11px;color:#9ca3af;margin-top:18px;
           border-top:1px solid #eee;padding-top:12px;">
        Premium seats can vanish within minutes of a show being listed — treat
        this as "check now", not a reservation. Automated alert · BMS Notifier.</div>
    </td></tr>
  </table>
</body>
</html>"""

    # ── Plain-text fallback (same information, links inline). ──────────────
    plain_lines = [f"{movie_name} — watch: {watch_label}", now_str, ""]
    if changes:
        plain_lines.append("CURRENT STATUS (new watch):" if bootstrap
                           else "WHAT CHANGED SINCE LAST ALERT:")
        plain_lines.extend(f"  - {change_line(c)}" for c in changes)
        plain_lines.append("")
    plain_lines.append("CURRENT SHOWTIMES:")
    for dc in sorted(by_date):
        plain_lines.append(f"\n{fmt_date(dc)}  ->  {booking_url(watch, dc)}")
        for s in by_date[dc]:
            fmt = f" [{s.screen_attr}]" if s.screen_attr else ""
            cats = " | ".join(
                f"{c.name} ₹{c.price} ({_cat_status_label(c.status)})"
                for c in s.categories
            )
            plain_lines.append(f"  {s.time}{fmt} - {cats}")
    if not by_date:
        plain_lines.append("  (no matching showtimes listed yet)")
    plain_lines.extend(["", "Premium seats can vanish within minutes of listing.",
                        "Automated alert from BMS Notifier."])
    plain = "\n".join(plain_lines)

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": frm, "to": [to],
                "subject": subject,
                "text": plain, "html": html,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"  ✅ Email sent to {to}")
        else:
            print(f"  ❌ Resend {resp.status_code}: {resp.text}")
            sys.exit(1)
    except requests.RequestException as e:
        print(f"  ❌ Email failed: {e}")
        sys.exit(1)


# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────
def fetch_event(event_code, region, date_list):
    """Fetch one event across date_list.

    Returns (shows, dates, movie, ok_fetches, failed_dates) where failed_dates
    is the set of specific date codes whose fetch failed this poll — the caller
    carries those dates' shows forward so a transient 403 doesn't drop them and
    re-report them as new next poll.
    """
    region_code, region_slug_r, lat, lon, geohash = region
    all_shows, all_dates = [], []
    movie_info = {"name": "Unknown", "language": ""}
    ok_fetches = 0
    failed_dates = set()
    for dc in date_list:
        data = fetch_bms(event_code, dc, region_code,
                         region_slug_r, lat, lon, geohash)
        if not data:
            print(f"  ⚠️  No data for {event_code} date {dc or '(default)'}")
            if dc:
                failed_dates.add(dc)
            continue
        ok_fetches += 1
        if movie_info["name"] == "Unknown":
            movie_info = parse_movie_info(data)
        all_dates.extend(parse_dates(data))
        all_shows.extend(parse_shows(data))
    return all_shows, all_dates, movie_info, ok_fetches, failed_dates


def check_once(watches, old_state):
    """Run one poll across every watch.

    Watches are grouped by event code so each event is fetched once. Returns
    new state keyed by watch name, or None if EVERY event failed this poll (a
    transient signal the caller escalates only after repeated failures). If
    some events succeed and others fail, the failed event's watches keep their
    previous state so a single flaky 403 does not erase a baseline.
    """
    old_state = old_state or {}
    groups = {}
    for w in watches:
        groups.setdefault(w["event_code"], []).append(w)

    new_state = {}
    events_ok = 0
    for event_code, group in groups.items():
        region = group[0]["region"]  # same event code → same region
        wanted = set()
        for w in group:
            wanted |= parse_date_codes(w["dates"])
        date_list = [""] + sorted(wanted) if wanted else [""]

        all_shows, all_dates, movie_info, ok, failed_dates = fetch_event(
            event_code, region, date_list)

        if not ok or not all_dates:
            # This event was unreadable this poll. Carry its watches' prior
            # state forward untouched rather than dropping their baselines.
            print(f"  ⚠️  {event_code}: no data this poll — carrying state.")
            for w in group:
                if w["name"] in old_state:
                    new_state[w["name"]] = old_state[w["name"]]
            continue
        events_ok += 1

        print(f"  🎬 {movie_info['name']}  {movie_info['language']}  "
              f"[{event_code}]")

        for w in group:
            _check_watch(w, all_shows, all_dates, movie_info,
                         old_state, new_state, failed_dates)

    if not events_ok:
        print("  ⚠️  Every event failed this poll.")
        return None

    save_state(new_state)
    return new_state


def _check_watch(w, all_shows, all_dates, movie_info, old_state, new_state,
                 failed_dates=frozenset()):
    """Filter one watch against its event's data and alert on changes."""
    filtered = filter_shows(
        all_shows, w["theatre"], w["time"], w["dates"], w["screen"],
    )
    # The default view and a targeted view can return the same showtime.
    seen = set()
    deduped = []
    for s in filtered:
        key = (s.venue_code, s.session_id, s.date_code, s.time)
        if key not in seen:
            seen.add(key)
            deduped.append(s)
    filtered = deduped

    print(f"  ── {w['name']}: {len(filtered)} showtime(s) after filters")

    if all_shows and not filtered:
        warn_empty_filter(all_shows, w)

    watched_dates = parse_date_codes(w["dates"])
    slice_new = build_state(filtered, all_dates, watched_dates)

    slice_old = old_state.get(w["name"])

    # Carry forward shows/date-status for any watched date whose fetch FAILED
    # this poll. Without this, BMS's intermittent 403s drop that date's shows,
    # which then re-register as "new" on the next successful poll — the cause
    # of duplicate alerts firing over and over for the same shows.
    if slice_old and failed_dates:
        for key, val in slice_old.get("shows", {}).items():
            if val.get("date") in failed_dates and key not in slice_new["shows"]:
                slice_new["shows"][key] = val
        for dc, st in slice_old.get("dates", {}).items():
            if dc in failed_dates and DATE_OPENNESS.get(st, 0) > \
                    DATE_OPENNESS.get(slice_new["dates"].get(dc), 0):
                slice_new["dates"][dc] = st

    new_state[w["name"]] = slice_new
    if slice_old is not None:
        changes = detect_changes(slice_old, slice_new)
    else:
        # First time this watch runs. Do NOT silently adopt the current state
        # as baseline — if the target is ALREADY live (a watched date open, or
        # shows already matching), the user set the watch up late and would
        # otherwise never hear about it. Alert on bootstrap.
        changes = bootstrap_changes(slice_new)

    if changes:
        print(f"\n  ⚡ {w['name']}: {len(changes)} change(s) detected:")
        for c in changes:
            print(f"     {change_line(c)}")
        # Subject leads with the single most useful thing, not a raw count.
        send_email(
            f"BMS Alert: {movie_info['name']} — {change_subject(changes)}",
            changes, filtered, movie_info, w,
        )
    else:
        print(f"     ✅ no changes")

    for s in filtered:
        cats = ", ".join(
            f"{c.name}=₹{c.price}"
            f"({AVAIL_STATUS_MAP.get(c.status, ('?', ''))[0]})"
            for c in s.categories
        )
        fmt = f"|{s.screen_attr}" if s.screen_attr else ""
        print(f"     {s.venue_name} — {s.time}{fmt} "
              f"[{s.date_code}] — {cats}")


# Give up only after this many polls in a row fail. Single failures are
# routine (BMS 403s intermittently); a sustained run of them is not.
MAX_CONSECUTIVE_FAILURES = 5


def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] BMS Ticket Checker — CI mode")

    try:
        watches = parse_watches()
    except (ValueError, json.JSONDecodeError) as e:
        print(f"  ❌ Bad watch config: {e}")
        sys.exit(1)

    for w in watches:
        print(f"    watch {w['name']}: event={w['event_code']} "
              f"dates={w['dates'] or 'any'} "
              f"theatre={w['theatre'] or 'any'} "
              f"screen={w['screen'] or 'any'}")

    # GitHub's scheduler will not start runs at the interval it is asked for
    # (measured: gaps of 1–4h against a */5 cron). So a run polls on its own
    # clock rather than relying on being started again promptly.
    loop_minutes = float(os.getenv("BMS_LOOP_MINUTES", "0") or 0)
    poll_seconds = float(os.getenv("BMS_POLL_SECONDS", "60") or 60)
    deadline = time.monotonic() + loop_minutes * 60

    state = load_state()
    polls = 0
    failures = 0

    while True:
        polls += 1
        if loop_minutes:
            print(f"\n  ── poll {polls} "
                  f"({datetime.now().strftime('%H:%M:%S')}) ──")

        new_state = check_once(watches, state)

        if new_state is None:
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                # Exit non-zero so the run goes red and GitHub notifies. A
                # watcher that cannot reach BMS is broken, but silently
                # reports "nothing to report" — indistinguishable from a
                # healthy run that found no changes.
                print(f"  ❌ {failures} consecutive failed polls — the "
                      f"watcher is NOT working. Check for blocking or a "
                      f"changed API.")
                sys.exit(1)
        else:
            failures = 0
            state = new_state

        remaining = deadline - time.monotonic()
        if remaining <= poll_seconds:
            break
        time.sleep(poll_seconds)

    print(f"\n  Done — {polls} poll(s).")


if __name__ == "__main__":
    main()