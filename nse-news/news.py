#!/usr/bin/env python3
"""News monitor for nse-news. Standard library only.

Watches headlines and NSE corporate announcements for the stocks you watch or
hold and prints only what is new since the last check, newest first, with how
far each stock has moved today.

    python3 news.py                                  # watchlist + plan + portfolio symbols
    python3 news.py --tickers RELIANCE TCS INFY
    python3 news.py --tickers RELIANCE --all --hours 72  # everything in the last 3 days, seen or not
    python3 news.py --no-filings                     # headlines only
    python3 news.py --no-moves                       # skip the price line per ticker

Your symbols, when --tickers is not given: every symbol in nse/watchlist.json
(nse-scanner), the symbol in nse/plan.json (nse-exit) and the positions in
nse/portfolio.json. Company names come from nse/universe.json (nse-scanner
universe), the file that maps SYMBOL to "Company Name Limited".

Sources:
  - Google News India RSS, one request per company, query "<Company Name>" share:
    https://news.google.com/rss/search?q=%22Reliance%20Industries%22%20share&hl=en-IN&gl=IN&ceid=IN:en
  - NSE corporate announcements RSS, one request for all companies. Each item's
    title is the company name and its description ends in "|SUBJECT: <category>".
    Items are matched to your symbols by exact normalised company name (lowercase,
    no trailing Limited/Ltd, no punctuation, "&" as "and"); no fuzzy matching:
    https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml
  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against the previous close:
    https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m

Times print in IST. NSE's own times are IST; Google's are GMT and are converted.
State: nse/state/news.json remembers what you have seen (30 days), so an item
shows once. Offline: --offline DIR reads google-<SYMBOL>.xml,
Online_announcements.xml and chart-<SYMBOL>.json from DIR. The tests use it.

Headlines are leads, not facts: open the story before acting, and treat
nothing here as advice.
"""

import argparse
import email.utils
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# --- nse common: begin ---
# Shared by every tool and kept identical by tests/sync_common.py. Edit it in nse-exit/exit.py only.
# IST clock, NSE calendar, tick sizes, INR formatting, Yahoo symbols. Imports are aliased so they
# never rebind a name the tool itself uses.
import datetime as _dt
import re as _re
import urllib.parse as _urlparse
from decimal import Decimal as _Dec, ROUND_CEILING as _CEIL, ROUND_FLOOR as _FLOOR, ROUND_HALF_UP as _HALF_UP

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
_UTC = _dt.timezone.utc
PREOPEN = _dt.time(9, 0)
OPEN = _dt.time(9, 15)
CLOSE = _dt.time(15, 30)
CALENDAR_YEARS = (2026,)
# NSE CM trading holidays (weekdays only), from nseindia.com holiday-master?type=trading, fetched 2026-10-28.
NSE_HOLIDAYS = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}


def to_ist(utc):
    """An aware or naive (taken as UTC) datetime -> aware IST datetime."""
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=_UTC)
    return utc.astimezone(IST)


def ist_to_utc(local_naive):
    """A naive IST wall-clock datetime -> aware UTC datetime (an aware one is just converted)."""
    if local_naive.tzinfo is None:
        local_naive = local_naive.replace(tzinfo=IST)
    return local_naive.astimezone(_UTC)


def parse_now(text):
    """--now -> aware UTC datetime. 'YYYY-MM-DD HH:MM' or 'HH:MM' (today) is IST; 'Z' or an offset is honoured."""
    if not text or not str(text).strip():
        return _dt.datetime.now(_UTC).replace(microsecond=0)
    t = str(text).strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    if _re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (to_ist(_dt.datetime.now(_UTC)).date().isoformat(), t.zfill(5))
    if t.endswith(("Z", "z")):
        t = t[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(t)
    except ValueError:
        raise ValueError('--now wants an IST time like "2026-10-27 10:15" (or ...Z for UTC), not %r' % text)
    return ist_to_utc(dt)


def _special(d, cfg):
    sp = ((cfg or {}).get("special_sessions") or {}).get(d.isoformat())
    if not sp:
        return None
    try:
        a, b = [_dt.time(int(x[:2]), int(x[3:5])) for x in sp[:2]]
    except (ValueError, TypeError, IndexError):
        return None
    return a, b


def trading_day(d, cfg=None):
    """(is_open, reason). reason: '' | 'weekend' | holiday name | 'special session' | 'calendar not loaded'."""
    if _special(d, cfg):
        return True, "special session"
    if d.weekday() >= 5:
        return False, "weekend"
    if d.year not in CALENDAR_YEARS:
        return True, "calendar not loaded"
    name = NSE_HOLIDAYS.get(d.isoformat())
    return (False, name) if name else (True, "")


def session(d, cfg=None):
    """(open_time, close_time) in IST, or None if the exchange is shut all day."""
    sp = _special(d, cfg)
    if sp:
        return sp
    return (OPEN, CLOSE) if trading_day(d, cfg)[0] else None


def _stamp(d, t):
    return "%sT%02d:%02d+05:30" % (d.isoformat(), t.hour, t.minute)


def market_status(now_utc, cfg=None):
    """{'state': 'pre-open'|'open'|'closed', 'reason': str, 'next_open': 'YYYY-MM-DDTHH:MM+05:30'}."""
    now = to_ist(now_utc)
    d, t = now.date(), now.time().replace(second=0, microsecond=0)
    ok, why = trading_day(d, cfg)
    s = session(d, cfg)
    state, reason = "closed", why
    if s:
        reason = ""
        if s[0] <= t < s[1]:
            state = "open"
        elif not _special(d, cfg) and PREOPEN <= t < OPEN:
            state, reason = "pre-open", "pre-open session until %02d:%02d" % (OPEN.hour, OPEN.minute)
        else:
            reason = "before the open" if t < s[0] else "after the close"
    nxt = ""
    for i in range(0, 400):
        day = d + _dt.timedelta(days=i)
        ds = session(day, cfg)
        if ds and (i > 0 or t < ds[0]) and not (i == 0 and state == "open"):
            nxt = _stamp(day, ds[0])
            break
    return {"state": state, "reason": reason, "next_open": nxt}


def _dec(x):
    if isinstance(x, _Dec):
        return x
    if isinstance(x, float):
        return _Dec(repr(x))
    return _Dec(str(x).strip().replace(",", ""))


def tick_for(price):
    """NSE CM tick size for a reference price. Upper bound of each slab is inclusive, except 250."""
    p = _dec(price)
    if p < 250:
        return _Dec("0.01")
    for top, tick in ((1000, "0.05"), (5000, "0.10"), (10000, "0.50"), (20000, "1.00")):
        if p <= top:
            return _Dec(tick)
    return _Dec("5.00")


def _to_tick(x, ref, mode):
    tick = tick_for(ref)
    n = (_dec(x) / tick).to_integral_value(rounding=mode)
    return (n * tick).quantize(_Dec("0.01"))


def floor_tick(x, ref):
    return _to_tick(x, ref, _FLOOR)


def round_tick(x, ref):
    return _to_tick(x, ref, _HALF_UP)


def ceil_tick(x, ref):
    return _to_tick(x, ref, _CEIL)


def inr(x, decimals=2):
    """Rupees with Indian digit grouping: 12345678.9 -> '₹1,23,45,678.90'."""
    v = _dec(x).quantize(_Dec(1).scaleb(-decimals), rounding=_HALF_UP)
    sign = "-" if v < 0 else ""
    whole, _, frac = format(abs(v), "f").partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return "%s₹%s%s" % (sign, whole, "." + frac if frac else "")


def crore(x):
    """1.9407e11 -> '₹19,407.0 cr'."""
    return inr(_dec(x) / _Dec(10000000), 1) + " cr"


def yahoo_symbol(sym):
    """NSE symbol -> Yahoo symbol; '&' and '-' stay: M&M -> M&M.NS."""
    s = str(sym).strip().upper()
    return s if s.endswith((".NS", ".BO")) else s + ".NS"


def yahoo_quote(sym):
    """yahoo_symbol, URL-encoded for a path: M&M -> M%26M.NS."""
    return _urlparse.quote(yahoo_symbol(sym), safe="")
# --- nse common: end ---

GOOGLE = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
ANNOUNCEMENTS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
UA = "Mozilla/5.0 (nse-day-trader-skill)"
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a stock this far from the previous close is flagged as moving
NSE_PAUSE = 0.25  # seconds between requests to NSE

# SUBJECT text (case-insensitive substring) -> tag. More than one can match; none -> "filing".
# "Outcome of Board Meeting" is listed before "Board Meeting" (same tag, kept in this order).
SUBJECT_TAGS = [
    ("Financial Result", "earnings"),
    ("Outcome of Board Meeting", "board"),
    ("Board Meeting", "board"),
    ("Acquisition", "deal"),
    ("Amalgamation", "deal"),
    ("Resignation", "management"),
    ("Appointment", "management"),
    ("Credit Rating", "rating"),
    ("Analysts/Institutional Investor Meet", "analyst"),
    ("Analyst/Investor Meet", "analyst"),
]
IMPORTANT_TAGS = {"earnings", "deal", "management", "rating"}
KEYWORDS = [
    ("earnings", r"\bearnings\b|\bresults?\b|\bQ[1-4](?:\s?FY\d{2})?\b|\bEPS\b|\bguidance\b|\bnet profit\b|\bPAT\b"),
    ("board", r"\bboard meeting\b|\bboard approves\b|\bboard clears\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|target price|\bprice target\b|\bbrokerage\b|\bbuy rating\b"),
    ("legal", r"\blawsuit\b|\bsued\b|\bprobe\b|\binvestigation\b|\bNCLT\b|\bED raid|\bCBI\b|\bcourt\b|\bSupreme Court\b|\bHigh Court\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bbuyout\b|\btakeover\b|\bstake sale\b|\bdeal\b|\bamalgamation\b"),
    ("capital", r"\bdividend\b|\bbuyback\b|\bbonus\b|\bstock split\b|\bright[s]? issue\b|\bQIP\b|\bIPO\b|\bfund ?raise\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bMD\b|\bresign|\bsteps down\b|\bappoint"),
    ("risk", r"\brecall\b|\bbankrupt|\binsolvency\b|\bdefault\b|\bdelist|\bhack|\bbreach\b|\boutage\b|\bfraud\b"),
    ("halt", r"\bhalt(?:s|ed)?\b|\btrading (?:pause|suspen)|\bupper circuit\b|\blower circuit\b|\bASM\b|\bGSM\b"),
    ("regulatory", r"\bSEBI\b|\bapproval\b|\bapproves\b|\bCCI\b|\bFSSAI\b|\bUSFDA\b|\bDGCA\b|\bTRAI\b"),
    ("macro", r"\bRBI\b|\brepo rate\b|\bCPI\b|\binflation\b|\bGDP\b|\bunion budget\b|\btariffs?\b|\bcrude\b|\bGIFT Nifty\b|\bFPI\b|\bFII\b"),
]


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- names

def normalise_name(name):
    """Company name -> comparison key: lowercase, no trailing Limited/Ltd, no punctuation, '&' as 'and'."""
    s = str(name or "").lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s(?:limited|ltd)$", "", s)
    return s


def strip_limited(name):
    """'Reliance Industries Limited' -> 'Reliance Industries' (the form people write in headlines)."""
    return re.sub(r"[\s,]+(?:limited|ltd)\.?\s*$", "", str(name).strip(), flags=re.I).strip()


def google_url(name):
    q = '"%s" share' % strip_limited(name)
    return GOOGLE % urllib.parse.quote(q, safe="")


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="nse/portfolio.json", watchlist="nse/watchlist.json",
                     plan="nse/plan.json", universe="nse/universe.json"):
    """{SYMBOL: company name or None}: the scanner's symbols, the planned trade and your holdings."""
    names = (load_json(universe) or {}).get("names") or {}
    out = {}

    def add(sym):
        if sym:
            out[str(sym).upper()] = names.get(str(sym).upper())

    for h in (load_json(watchlist) or {}).get("symbols", []):
        add(h.get("symbol") if isinstance(h, dict) else h)
    add((load_json(plan) or {}).get("symbol"))
    for p in (load_json(portfolio) or {}).get("positions", []):
        add(p.get("symbol") or p.get("ticker"))
    return out


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=NSE_PAUSE):
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url, exc.reason))


# ---------------------------------------------------------------- parsing

MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
NSE_TIME = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?$")


def parse_time(value):
    """NSE '28-Oct-2026 21:01:36' (IST), RFC 822 (Google, GMT) or ISO 8601 -> aware IST datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    m = NSE_TIME.match(value)
    if m and m.group(2).lower() in MONTHS:
        d, mon, y, hh, mm, ss = m.groups()
        return _dt.datetime(int(y), MONTHS[mon.lower()], int(d), int(hh), int(mm), int(ss or 0), tzinfo=IST)
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return to_ist(d)


def _local(tag_name):
    return tag_name.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def _root(data, what):
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("%s is not valid XML: %s" % (what, exc))


def tag_subject(subject):
    """NSE announcement SUBJECT -> tags (case-insensitive substrings; none matched -> ['filing'])."""
    low = str(subject or "").lower()
    out = [t for key, t in SUBJECT_TAGS if key.lower() in low]
    seen = set()
    out = [t for t in out if not (t in seen or seen.add(t))]
    return out or ["filing"]


def headline_tags(text):
    return [label for label, rx in KEYWORDS if re.search(rx, text, re.I)]


def parse_announcements(data):
    """NSE announcements RSS -> items (not yet matched to a ticker)."""
    root = _root(data, "NSE announcements feed")
    out = []
    for it in root.iter("item"):
        company = _text(it, "title")
        desc = _text(it, "description")
        body, _, subject = desc.partition("|SUBJECT:")
        subject = subject.strip()
        body = body.strip()
        link = _text(it, "link")
        title = "%s (%s)" % (body, subject) if body and subject else (body or subject or company)
        out.append({"id": "nse:" + (link or "%s|%s" % (company, _text(it, "pubDate"))),
                    "source": "NSE announcement", "kind": "filing", "company": company,
                    "norm": normalise_name(company), "subject": subject, "title": title,
                    "tags": tag_subject(subject), "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def title_key(title):
    return re.sub(r"\W+", " ", title.lower()).strip()


def parse_google(data, ticker):
    """Google News RSS for one company query -> items for `ticker`."""
    root = _root(data, "Google News feed for %s" % ticker)
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        src = _text(it, "source")
        suffix = " - " + src
        if src and title.endswith(suffix):
            title = title[:-len(suffix)].strip()
        elif " - " in title:
            title, _, tail = title.rpartition(" - ")
            src = src or tail.strip()
        out.append({"id": "g:" + title_key(title), "source": src or "Google News", "kind": "headline",
                    "ticker": ticker, "title": title, "link": _text(it, "link"),
                    "time": parse_time(_text(it, "pubDate"))})
    return out


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": to_ist(_dt.datetime.fromtimestamp(when, _UTC)) if when else None}


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        name = "chart-%s.json" % t
        try:
            try:
                data = fetcher.get(CHART % ("query2", yahoo_quote(t)), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", yahoo_quote(t)), name)
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def tag(items):
    """Headline items get keyword tags; every item lists the tickers it touches."""
    for it in items:
        if it["kind"] == "headline":
            it["tags"] = headline_tags(it["title"])
        it["touches"] = [it["ticker"]]
        it["important"] = bool(it["kind"] == "filing" and set(it["tags"]) & IMPORTANT_TAGS)
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same normalised title."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"], "t:" + title_key(it["title"])]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    key = it["id"] if it["kind"] == "filing" else "%s|%s" % (it["id"], it["ticker"])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, filings=True, errors=None):
    errors = [] if errors is None else errors
    items = []

    def quiet(exc):
        return fetcher.offline and "no file" in str(exc)

    for t in sorted(watch):
        name = watch[t]
        if not name:
            errors.append("no company name for %s in the universe file: headlines skipped "
                          "(run nse-scanner universe)" % t)
            continue
        try:
            items += parse_google(fetcher.get(google_url(name), "google-%s.xml" % t), t)
        except NewsError as exc:
            if not quiet(exc):
                errors.append("headlines for %s: %s" % (t, exc))
    if filings:
        by_name = {}
        for t, name in watch.items():
            if name:
                by_name.setdefault(normalise_name(name), []).append(t)
        try:
            for it in parse_announcements(fetcher.get(ANNOUNCEMENTS, "Online_announcements.xml")):
                for t in sorted(by_name.get(it["norm"], [])):
                    items.append(dict(it, ticker=t))
        except NewsError as exc:
            if not quiet(exc):
                errors.append("NSE announcements: %s" % exc)
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s IST" % to_ist(m["time"]).strftime("%H:%M")) if m.get("time") else ""
        out.append("%-11s %s %+.2f%% vs previous close %s%s  %s" % (
            m["ticker"], inr(m["price"]), m["pct"], inr(m["prev_close"]), when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = to_ist(it["time"]).strftime("%Y-%m-%d %H:%M IST") if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-11s %-17s %s" % (mark, when, who, it["source"], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % to_ist(now).strftime("%Y-%m-%d %H:%M IST"))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="NSE symbols; default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="nse/portfolio.json")
    ap.add_argument("--watchlist", default="nse/watchlist.json")
    ap.add_argument("--plan", default="nse/plan.json")
    ap.add_argument("--universe", default="nse/universe.json", help="SYMBOL -> company name (nse-scanner universe)")
    ap.add_argument("--state", default="nse/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--no-filings", action="store_true", help="headlines only (skip NSE announcements)")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per ticker")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help='IST time "YYYY-MM-DD HH:MM", for reproducible runs')
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    known = watch_from_files(args.portfolio, args.watchlist, args.plan, args.universe)
    if args.tickers:
        names = (load_json(args.universe) or {}).get("names") or {}
        watch = {t.upper(): names.get(t.upper()) for t in args.tickers}
    else:
        watch = known
    if not watch:
        print("news: no symbols. Pass --tickers, or run /nse-scanner first (it writes "
              "nse/watchlist.json).", file=sys.stderr)
        return 2
    try:
        now = parse_now(args.now)
    except ValueError as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 2
    state = load_json(args.state) or {}
    errors = []
    fetcher = Fetcher(offline=args.offline)
    items = collect(fetcher, watch, filings=not args.no_filings, errors=errors)
    items = dedupe(tag(items))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": to_ist(now).isoformat(), "moves": mv, "items": _json_ready(shown),
                          "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
