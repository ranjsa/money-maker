#!/usr/bin/env python3
"""Chart Scanner for nse-scanner: scan every NSE EQ chart for setups. Standard library only.

    python3 scan.py universe                          # NSE EQUITY_L.csv -> nse/universe.json
    python3 scan.py daily                             # before the open: daily setups -> nse/watchlist.json
    python3 scan.py daily --symbols RELIANCE,TCS,M&M  # or --watchlist FILE, or --limit 200
    python3 scan.py orb                               # after 09:35 IST: opening-range breakouts so far
    python3 scan.py orb --at 10:15 --or-minutes 15    # cut the clock at 10:15 IST
    python3 scan.py orb --offline chart-5m.json.gz --now "2026-10-27 10:15"

universe  Downloads NSE's EQUITY_L.csv (nsearchives.nseindia.com) and keeps the
          series EQ rows. BE and BZ (trade-to-trade, no intraday), SM, ST and
          the rest are out. Writes {"as_of", "symbols", "names"} to
          nse/universe.json. The file's headers carry leading spaces; they are
          stripped.

daily     Pre-market scan of daily CLOSES (Yahoo spark: 20 symbols a request,
          3 months of 1-day bars, closes only). Liquidity: last price >=
          --min-price (20) and turnover (last close x last session's volume)
          >= --min-turnover-inr (5,00,00,000, that is Rs 5 crore). Setups,
          from closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until the session's close (15:30 IST).

orb       Opening-range breakouts, 5-minute bars. The range is the first
          --or-minutes (15) of the session, 09:15 to 09:30 IST. Stage 1: spark
          closes for every symbol (5-minute bars, 20 a request); a chart goes
          on only if a completed bar after the range closed at or above the
          highest close of the range (a necessary condition). Stage 2: each
          candidate's v8 chart (real highs, lows, volume): range high = the
          highest high of the range bars, a hit = the first completed bar to
          CLOSE above it, at that bar's close time. Stage 2 fetches at most
          --max-confirm (200) charts, the most traded candidates first, and
          says so when it stops short. A chart is usable only when every
          5-minute bar from 09:15 to the cutoff is there (a missing bar is a
          halt or no trades). Floors: --min-price 20, --min-turnover-inr
          5,00,00,000 (close x volume summed over the session bars so far).

Bars: null closes are dropped, a bar off the 5-minute grid (the live tick) is
dropped, and a bar counts only once it is complete (start + 5 minutes <= the
clock). All times are IST (UTC+05:30, no DST). --now pins the clock
("2026-10-27 10:15" is IST; ...Z for UTC). `--at 09:35` means the bars
completed by 09:35 IST: the 09:30 bar counts.

On a holiday or weekend orb prints "market closed: <holiday>" and exits 1
without fetching. A year the calendar does not hold (outside 2026) is treated
as Mon-Fri with a warning.

Yahoo's endpoints are free, unofficial and can lag. query2 first, query1 on a
429 or 5xx, one retry after a pause on a 429. At least 0.25 s between
requests (--pause). Partial results are reported as partial.

--offline PATH reads files instead of the network: universe wants a folder with
EQUITY_L.csv (or the csv itself); daily and orb take a folder of <SYMBOL>.json
v8 chart files or spark responses, or one file that is a spark response, a v8
chart, or a bundle {"RELIANCE": <v8 json>, ...}. .json.gz is fine.

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
"""

import argparse
import csv
import datetime
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

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

UA = "Mozilla/5.0 (nse-day-trader-skill)"
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
EQUITY_FILE = "EQUITY_L.csv"
EQUITY_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
SPARK_MAX = 20      # Yahoo's spark answers 400 above 20 symbols
PAUSE = 0.3         # seconds between requests: be polite to free endpoints
MIN_PAUSE = 0.25    # never faster than this, whatever --pause says
BACKOFF = 5.0       # seconds to wait before the one retry after a 429
SLEEP = time.sleep  # tests swap this out
BAR_MIN = 5
MAX_CONFIRM = 200   # stage-2 v8 charts per orb scan, most traded first
MIN_DAILY_BARS = 30
MIN_PRICE = 20
MIN_TURNOVER_INR = 50000000   # Rs 5 crore
DEFAULT_UNIVERSE = "nse/universe.json"
DEFAULT_WATCHLIST = "nse/watchlist.json"
DEFAULT_CONFIG = "nse/config.json"
DEFAULT_NO_ENTRIES_AFTER = "14:45"
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- time (IST)

def local(now_utc):
    """Aware or naive-UTC datetime -> naive IST wall clock."""
    return to_ist(now_utc).replace(tzinfo=None)


def ts_to_ist(ts):
    """Unix seconds -> naive IST. Never uses the machine's time zone."""
    return datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts) + 19800)


def now_from(text):
    try:
        return parse_now(text)
    except ValueError as e:
        raise ScanError(str(e))


def parse_hhmm(text, flag="--at"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScanError("%s wants an IST time like 10:15" % flag)
    return datetime.time(int(m.group(1)), int(m.group(2)))


def clock_label(t):
    """datetime or time -> '10:15 IST' (24 hour)."""
    return "%02d:%02d IST" % (t.hour, t.minute)


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


def calendar_warning(d, cfg=None):
    """'' or the one-line warning for a year the holiday list does not hold."""
    if trading_day(d, cfg)[1] == "calendar not loaded":
        return ("NSE calendar for %d not loaded: run scan.py calendar --refresh or check nseindia.com "
                "(treating Mon-Fri as open)" % d.year)
    return ""


def polite(pause):
    return max(float(pause or 0), MIN_PAUSE)


# ---------------------------------------------------------------- network

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_get(path):
    """GET a Yahoo Finance path. query2 first, query1 on a 429 or 5xx, and one
    more round after BACKOFF seconds if a 429 was seen. 4xx other than 429 is final."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                try:
                    body = e.read().decode("utf-8", "replace") if 400 <= e.code < 500 and e.code != 429 else ""
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code == 429:
                    saw_429 = True
                    continue
                if e.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body  # Yahoo's JSON error ("No data found"), parsed by the caller
                raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))


def spark_path(symbols, rng, interval):
    return "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
        ",".join(yahoo_quote(s) for s in symbols), rng, interval)


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (yahoo_quote(sym), rng, interval)


# ---------------------------------------------------------------- parsing

def series_from_result(res):
    """A Yahoo chart result (v8 or one spark response) -> plain lists."""
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = q.get(k)
        return list(v) + [None] * (n - len(v)) if isinstance(v, list) else None
    return {"t": list(t), "c": col("close") or [None] * n, "o": col("open"), "h": col("high"),
            "l": col("low"), "v": col("volume"), "meta": res.get("meta") or {}}


def plain_symbol(ysym):
    """Yahoo symbol -> NSE symbol: RELIANCE.NS -> RELIANCE, M&M.NS -> M&M."""
    s = ysym.strip().upper()
    for suffix in (".NS", ".BO"):
        if s.endswith(suffix):
            return s[:-len(suffix)]
    return s


def parse_spark(text_or_doc, wanted=None):
    """Spark JSON -> {SYM: series}. Symbols Yahoo does not know are simply missing
    from the answer. `wanted` maps Yahoo symbols back to ours."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    err = sp.get("error")
    out = {}
    for item in sp.get("result") or []:
        resp = item.get("response") or []
        if not resp:
            continue
        ysym = item.get("symbol") or (resp[0].get("meta") or {}).get("symbol") or ""
        sym = (wanted or {}).get(ysym.upper(), plain_symbol(ysym))
        out[sym] = series_from_result(resp[0])
    if err and not out:
        raise ScanError("Yahoo spark: %s" % (err.get("description") or err.get("code") or err))
    return out


def parse_chart(text_or_doc, sym=""):
    """v8 chart JSON -> series. Raises ScanError for Yahoo's 'no data' answers."""
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise ScanError("no data for %s: %s" % (sym, err.get("description", "unknown ticker")))
    return series_from_result(res[0])


def _read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _absorb(doc, name_hint, data, errors):
    """One parsed offline JSON doc -> data / errors."""
    if isinstance(doc, dict) and "spark" in doc:
        data.update(parse_spark(doc))
    elif isinstance(doc, dict) and "chart" in doc:
        sym = name_hint
        if not sym:
            try:
                sym = (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"]
            except (AttributeError, KeyError, TypeError, IndexError):
                raise ScanError("an offline v8 chart without a symbol: name the file <SYMBOL>.json")
        sym = plain_symbol(sym)
        try:
            data[sym] = parse_chart(doc, sym)
        except ScanError as e:
            errors[sym] = str(e)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % name_hint)


def load_offline(path):
    """A folder of <SYMBOL>.json(.gz) files or one file -> ({SYM: series}, {SYM: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            if isinstance(doc, dict) and ("chart" in doc or "spark" in doc):
                _absorb(doc, "" if "spark" in doc else _stem(f), data, errors)
            else:
                _absorb(doc, "", data, errors)
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        _absorb(doc, "", data, errors)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- universe

def parse_equity_list(text):
    """NSE EQUITY_L.csv -> [{symbol, name, series, isin, listed}]. The header cells carry
    leading spaces (' SERIES', ' DATE OF LISTING'); they and every value are stripped."""
    text = text.lstrip("﻿").replace("\r", "")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ScanError("not an NSE EQUITY_L.csv (empty)")
    hdr = [h.strip().upper() for h in rows[0]]
    if "SYMBOL" not in hdr or "SERIES" not in hdr:
        raise ScanError("not an NSE EQUITY_L.csv (header %s)" % ",".join(hdr)[:60])
    ix = {k: (hdr.index(c) if c in hdr else None) for k, c in
          (("symbol", "SYMBOL"), ("name", "NAME OF COMPANY"), ("series", "SERIES"),
           ("isin", "ISIN NUMBER"), ("listed", "DATE OF LISTING"))}
    out = []
    for r in rows[1:]:
        if not r or len(r) <= max(i for i in ix.values() if i is not None):
            continue
        row = {k: (" ".join(r[i].split()) if i is not None else "") for k, i in ix.items()}
        if row["symbol"]:
            out.append(row)
    return out


def build_universe(text):
    """EQUITY_L.csv text -> sorted symbols of series EQ only (BE/BZ are trade-to-trade: no intraday)."""
    return sorted({r["symbol"].upper() for r in parse_equity_list(text) if r["series"].upper() == "EQ"})


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_bars(series, now_utc, cfg=None):
    """[(date, close, volume)] of COMPLETE daily bars: null closes dropped, today's bar dropped
    until the session closes (15:30 IST), anything dated after the clock dropped."""
    now = local(now_utc)
    today = now.date()
    s = session(today, cfg)
    today_done = s is None or now.time() >= s[1]
    vols = series.get("v")
    out = {}
    for i, (ts, c) in enumerate(zip(series["t"], series["c"])):
        if c is None:
            continue
        d = ts_to_ist(ts).date()
        if d > today or (d == today and not today_done):
            continue
        out[d] = (float(c), vols[i] if vols and i < len(vols) else None)  # one bar a day: a late duplicate wins
    return [(d, c, v) for d, (c, v) in sorted(out.items())]


def daily_closes(series, now_utc, cfg=None):
    return [(d, c) for d, c, _ in daily_bars(series, now_utc, cfg)]


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def scan_daily(data, symbols, now_utc, min_price=MIN_PRICE, min_turnover=MIN_TURNOVER_INR, cfg=None):
    """Pure. data = {SYM: series}. Returns (hits, counts, last_date). Turnover is the last complete
    session's close x volume (from the bar when the series has volumes, else the spark meta)."""
    hits, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_bars(s, now_utc, cfg)
        meta = s.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None and bars:
            price = bars[-1][1]
        vol = bars[-1][2] if bars and bars[-1][2] is not None else meta.get("regularMarketVolume")
        turnover = (bars[-1][1] * vol) if bars and vol is not None else None
        if (price is None or price < min_price
                or (min_turnover > 0 and (turnover is None or turnover < min_turnover))):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c, _ in bars]
        tags, nums = find_setups(closes)
        if tags:
            h = {"symbol": sym, "setups": tags, "price": round(closes[-1], 2),
                 "close_date": d_last.isoformat(), "rsi": round(nums["rsi"], 1),
                 "volume": int(vol) if vol is not None else None, "turnover_inr": round(turnover, 2)}
            if "breakout_20d" in tags:
                h["level"] = round(nums["high20"], 2)
            hits.append(h)
    hits.sort(key=lambda h: (-h["turnover_inr"], h["symbol"]))  # most rupees traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- opening-range breakout

def _sess(day, cfg=None):
    return session(day, cfg) or (OPEN, CLOSE)


def slot(day, k, cfg=None):
    return datetime.datetime.combine(day, _sess(day, cfg)[0]) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(series, day, cutoff, cfg=None):
    """5-minute bars of `day` that are complete at `cutoff` (naive IST), on the grid, with a
    close, inside the session. Sorted, one per slot."""
    open_t, close_t = _sess(day, cfg)
    out = {}
    hs, ls = series.get("h"), series.get("l")
    vs = series.get("v")
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        et = ts_to_ist(ts)
        if et.date() != day or et.second or et.minute % BAR_MIN:
            continue  # another day, or the live tick off the grid
        if et.time() < open_t or et.time() >= close_t:
            continue  # pre-open, after hours
        if et + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        out[et] = {"start": et, "c": float(c),
                   "h": None if not hs or hs[i] is None else float(hs[i]),
                   "l": None if not ls or ls[i] is None else float(ls[i]),
                   "v": (vs[i] or 0) if vs else 0}
    return [out[k] for k in sorted(out)]


def usable(bars, day, cutoff, n_range, cfg=None):
    """Every slot from the open to the cutoff is there, and at least the range."""
    open_t, close_t = _sess(day, cfg)
    first = datetime.datetime.combine(day, open_t)
    want = int((cutoff - first).total_seconds() // (BAR_MIN * 60))
    want = min(want, int((datetime.datetime.combine(day, close_t) - first).total_seconds() // (BAR_MIN * 60)))
    if want < n_range or len(bars) != want:
        return False
    return all(b["start"] == slot(day, k, cfg) for k, b in enumerate(bars))


def orb_candidate(bars, n_range):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range close)."""
    top = max(b["c"] for b in bars[:n_range])
    return any(b["c"] >= top for b in bars[n_range:])


def orb_hit(bars, n_range):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    if any(b["h"] is None or b["l"] is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            return {"close": round(b["c"], 2), "price": round(b["c"], 2), "at": closes_at.strftime("%H:%M"),
                    "bar_start": b["start"].strftime("%H:%M"), "bar_index": k,
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((b["c"] - oh) / oh * 100, 2), "_t": closes_at}
    return None


def orb_window(now_utc, at, or_minutes, until, explicit_day=None, cfg=None):
    """-> (day, cutoff naive IST) or raise ScanError with the plain message."""
    now = local(now_utc)
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    ok, why = trading_day(day, cfg)
    if not ok:
        raise ScanError("market closed: %s (%s). Run orb on a trading day after the opening range, "
                        "or replay a past day with --offline." % (why, day_label(day)))
    open_t, close_t = _sess(day, cfg)
    range_end = slot(day, or_minutes // BAR_MIN, cfg)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = datetime.datetime.combine(day, close_t)
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s)." % (at.strftime("%H:%M"), clock_label(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (at.strftime("%H:%M"), clock_label(close_dt),
                                                                       day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The market closed at %s on %s. Replay the session with --at 10:15."
                            % (clock_label(close_dt), day_label(day)))
    if cutoff < range_end:
        raise ScanError("It is %s: opening range not complete until %s (the %d-minute range runs %s to %s). "
                        "The first bar after it closes at %s. Run orb again after %s."
                        % (clock_label(cutoff), clock_label(range_end), or_minutes, clock_label(datetime.datetime.combine(day, open_t)),
                           clock_label(range_end), clock_label(first_close), clock_label(first_close)))
    if cutoff < first_close:
        raise ScanError("It is %s: the opening range is complete but the first bar after it closes at %s. "
                        "Run orb again after %s." % (clock_label(cutoff), clock_label(first_close),
                                                     clock_label(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 10:15." % (clock_label(cutoff), until.strftime("%H:%M"),
                                                      clock_label(first_close)))
    return day, cutoff


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_turnover=0, confirm=None,
             max_confirm=None, cfg=None):
    """stage1 = {SYM: series} with closes (spark or v8). confirm(sym) -> series with highs/lows
    (None = use the stage-1 series). With confirm and max_confirm, only the max_confirm most
    traded candidates (turnover so far) are confirmed; the rest are counted as unconfirmed.
    Turnover = sum(close x volume) over the session bars so far (a spark series has no volumes:
    its meta session volume x the last close stands in). Returns (hits, counts, failed {sym: why})."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    for sym in symbols:
        s = stage1.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, day, cutoff, cfg)
        if not usable(bars, day, cutoff, n_range, cfg):
            counts["unusable"] += 1
            continue
        if s.get("v"):
            turnover = sum(b["c"] * b["v"] for b in bars)
        else:
            turnover = bars[-1]["c"] * ((s.get("meta") or {}).get("regularMarketVolume") or 0)
        if bars[-1]["c"] < min_price or turnover < min_turnover:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if orb_candidate(bars, n_range):
            cands.append((sym, bars, turnover))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[2])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, bars, _ in cands:
        full = bars
        if confirm is not None:
            try:
                s2 = confirm(sym)
            except ScanError as e:
                failed[sym] = str(e)
                continue
            full = session_bars(s2, day, cutoff, cfg)
            if not usable(full, day, cutoff, n_range, cfg):
                failed[sym] = "the v8 chart has a missing bar"
                continue
        counts["confirmed"] += 1
        h = orb_hit(full, n_range)
        if h:
            hits.append(dict({"symbol": sym, "setups": ["orb"]}, **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(symbols, rng, interval, pause, a=None):
    """Batches of 20. -> ({SYM: series}, {SYM: error})."""
    data, failed = {}, {}
    batches = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for k, batch in enumerate(batches):
        if k:
            SLEEP(polite(pause))
        wanted = {yahoo_symbol(s).upper(): s for s in batch}
        try:
            got = parse_spark(yahoo_get(spark_path(batch, rng, interval)), wanted)
        except (ScanError, ValueError) as e:
            for s in batch:
                failed[s] = str(e)
            continue
        for s in batch:
            if s in got:
                data[s] = got[s]
            else:
                failed[s] = "no data from Yahoo"
        if a is not None and len(batches) > 25 and (k + 1) % 50 == 0:
            progress(a, "  spark %d of %d requests" % (k + 1, len(batches)))
    return data, failed


def read_symbols(a, offline_syms=None):
    if a.symbols:
        syms = [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif offline_syms is not None and not a.universe:
        syms = sorted(offline_syms)  # offline: every chart in the file, unless a list was given
    else:
        path = a.universe or DEFAULT_UNIVERSE
        if not os.path.exists(path):
            raise ScanError("no %s yet: run `scan.py universe` first, or pass --symbols" % path)
        syms = symbols_from_file(path)
    out = []
    for s in syms:
        if s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def symbols_from_file(path):
    """nse/universe.json, nse/watchlist.json, or a text file of SYMBOL or SYMBOL|Name lines."""
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = (doc.get("symbols") or doc.get("hits") or []) if isinstance(doc, dict) else doc
        return [str(x.get("symbol") if isinstance(x, dict) else x).upper() for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln.split("|")[0].strip().upper())
    return out


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _num(x):
    x = float(x)
    return int(x) if x == int(x) else x


def watch_entry(h):
    """A scan hit -> one nse/watchlist.json symbols[] row."""
    e = {"symbol": h["symbol"], "yahoo": yahoo_symbol(h["symbol"]), "close": h.get("close", h.get("price")),
         "setups": h["setups"]}
    for k in ("or_high", "or_low", "pct_over", "at", "level", "rsi", "turnover_inr"):
        if k in h:
            e[k] = h[k]
    return e


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    t0 = time.time()
    if a.offline:
        p = a.offline if os.path.isfile(a.offline) else os.path.join(a.offline, EQUITY_FILE)
        if not os.path.exists(p):
            raise ScanError("no %s in %s" % (EQUITY_FILE, a.offline))
        with open(p, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    else:
        try:
            text = http_get(EQUITY_URL, timeout=60)
        except urllib.error.HTTPError as e:
            raise ScanError("nsearchives.nseindia.com answered HTTP %d for %s" % (e.code, EQUITY_FILE))
        except (urllib.error.URLError, OSError) as e:
            raise ScanError("could not reach nsearchives.nseindia.com: %s" % getattr(e, "reason", e))
    rows = parse_equity_list(text)
    names = {}
    series = {}
    for r in rows:
        series[r["series"]] = series.get(r["series"], 0) + 1
        if r["series"].upper() == "EQ":
            names[r["symbol"].upper()] = r["name"]
    symbols = sorted(names)
    as_of = to_ist(parse_now(None)).strftime("%Y-%m-%dT%H:%M:%S+05:30")
    doc = {"as_of": as_of, "symbols": symbols, "names": {s: names[s] for s in symbols}}
    if not a.no_write:
        write_json(a.out, doc)
    dropped = {k: v for k, v in series.items() if k.upper() != "EQ"}
    if a.json:
        print(json.dumps({"count": len(symbols), "rows": len(rows), "series": series, "as_of": as_of,
                          "out": None if a.no_write else a.out, "seconds": round(time.time() - t0, 1)}, indent=2))
    else:
        print("NSE EQUITY_L.csv: %s rows." % "{:,}".format(len(rows)))
        print("Kept %s series EQ stocks (the only series that trade intraday)." % "{:,}".format(len(symbols)))
        if dropped:
            print("Dropped: " + ", ".join("%s %s" % ("{:,}".format(v), k) for k, v in
                                          sorted(dropped.items(), key=lambda kv: -kv[1]))
                  + " (BE and BZ are trade-to-trade: no intraday).")
        if not a.no_write:
            print("Wrote %s (symbols and names)." % a.out)
    return 0 if symbols else 1


def cmd_daily(a):
    t0 = time.time()
    now = now_from(a.now)
    cfg = read_config(a.config)
    warn = calendar_warning(local(now).date(), cfg)
    if warn:
        print(warn, file=sys.stderr)
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
    symbols = read_symbols(a, offline_syms)
    if not a.offline:
        progress(a, "Fetching daily closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, "3mo", "1d", a.pause, a)
    hits, counts, last_date = scan_daily(data, symbols, now, a.min_price, a.min_turnover_inr, cfg)
    nfailed = len(symbols) - counts["fetched"]
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    doc = {"created_by": "nse-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "date": local(now).date().isoformat(), "setup": "daily", "as_of": as_of, "universe": len(symbols),
           "scanned": counts["scanned"], "failed": nfailed, "source": "yahoo",
           "filters": {"min_price": _num(a.min_price), "min_turnover_inr": _num(a.min_turnover_inr)},
           "note": NOTE, "symbols": [watch_entry(h) for h in hits]}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2, ensure_ascii=False))
        return 0 if hits else 1
    print("Daily scan, %s %s, closes through %s (Yahoo spark, daily closes only)." % (
        day_label(local(now).date()), clock_label(local(now)), last_date.isoformat() if last_date else "nothing"))
    print("Fetched %s of %s charts%s. %s passed the liquidity filter (%s+ and %s+ traded)%s; "
          "%s scanned." % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
                           (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
                           "{:,}".format(counts["fetched"] - counts["illiquid"]), inr(a.min_price, 0),
                           crore(a.min_turnover_inr),
                           (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
                           "{:,}".format(counts["scanned"])))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (close x volume):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-12s %12s  RSI %5.1f  %s" % (h["symbol"], inr(h["price"]), h["rsi"],
                                               ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (setup daily, %d symbol%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /nse-news SYMBOL.")
    return 0 if hits else 1


def cmd_orb(a):
    t0 = time.time()
    now = now_from(a.now)
    at = parse_hhmm(a.at) if a.at else None
    cfg = read_config(a.config)
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or DEFAULT_NO_ENTRIES_AFTER, "--until")
    data = failed = None
    explicit_day = None
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [ts_to_ist(s["t"][-1]).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day, cfg)
    warn = calendar_warning(day, cfg)
    if warn:
        print(warn, file=sys.stderr)
    symbols = read_symbols(a, offline_syms)
    confirm = None
    if not a.offline:
        today = local(parse_now(None)).date()
        if (today - day).days > 6:
            raise ScanError("Yahoo's free 5-minute history is short: replay %s with --offline" % day.isoformat())
        rng = "1d" if day == today else "5d"
        progress(a, "Stage 1: 5-minute closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, rng, "%dm" % BAR_MIN, a.pause, a)
        done = []

        def confirm(sym):
            SLEEP(polite(a.pause))
            done.append(sym)
            if len(done) % 50 == 0:
                progress(a, "  stage 2: %d v8 charts so far" % len(done))
            return parse_chart(yahoo_get(chart_path(sym, rng, "%dm" % BAR_MIN)), sym)
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, a.min_turnover_inr,
                                     confirm, a.max_confirm, cfg)
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([s for s in symbols if s in failed])
    as_of = "%s %s IST" % (day.isoformat(), cutoff.strftime("%H:%M"))
    doc = {"created_by": "nse-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "date": day.isoformat(), "setup": "orb", "as_of": as_of, "universe": len(symbols),
           "scanned": counts["scanned"], "failed": nfailed, "unconfirmed": counts["unconfirmed"],
           "source": "yahoo", "or_minutes": a.or_minutes, "interval": "%dm" % BAR_MIN,
           "filters": {"min_price": _num(a.min_price), "min_turnover_inr": _num(a.min_turnover_inr)},
           "note": NOTE, "symbols": [watch_entry(h) for h in hits]}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2, ensure_ascii=False))
        return 0 if hits else 1
    open_t = _sess(day, cfg)[0]
    print("Opening-range breakout scan, %s %s: %d-minute range (%s to %s), 5-minute bars, Yahoo."
          % (day_label(day), clock_label(cutoff), a.or_minutes, open_t.strftime("%H:%M"),
             clock_label(slot(day, a.or_minutes // BAR_MIN, cfg))))
    print("Fetched %s of %s charts%s; %s usable%s; %s candidates, %s confirmed with real highs and lows."
          % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
             (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["scanned"]),
             (" (%s under the price / turnover floor)" % "{:,}".format(counts["illiquid"])) if counts["illiquid"] else "",
             "{:,}".format(counts["candidates"]), "{:,}".format(counts["confirmed"])))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % ("{:,}".format(counts["unconfirmed"]), "{:,}".format(a.max_confirm)))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s:"
              % (len(hits), ("the %s checked" % "{:,}".format(counts["confirmed"])) if counts["unconfirmed"]
                 else "{:,}".format(counts["scanned"]), clock_label(cutoff)))
        for h in hits[:a.show]:
            print("  %5s IST  %-12s %12s  range %s to %s  %+.2f%% over" % (
                h["at"], h["symbol"], inr(h["close"]), inr(h["or_low"]), inr(h["or_high"]), h["pct_over"]))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s." % clock_label(cutoff))
    if not a.no_write:
        print("Wrote %s (setup orb, %d symbol%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /nse-news SYMBOL and /nse-entry SYMBOL.")
    return 0 if hits else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chart Scanner (nse-scanner): daily setups and opening-range breakouts on NSE EQ.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="download NSE's EQUITY_L.csv -> %s" % DEFAULT_UNIVERSE)
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--offline", metavar="PATH", help="a folder with EQUITY_L.csv, or the csv itself")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")

    def common(p):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated NSE symbols instead of the universe (M&M, BAJAJ-AUTO)")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json or a text file of symbols")
        p.add_argument("--universe", help="symbol list: universe.json or one SYMBOL|Name a line (default %s)" % DEFAULT_UNIVERSE)
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=MIN_PRICE, help="rupees (%d)" % MIN_PRICE)
        p.add_argument("--min-turnover-inr", type=float, default=MIN_TURNOVER_INR,
                       help="rupees of close x volume (%d, Rs 5 crore)" % MIN_TURNOVER_INR)
        p.add_argument("--offline", metavar="PATH", help="fixture folder or file instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-27 10:15" IST, or ...Z)')
        p.add_argument("--out", default=DEFAULT_WATCHLIST)
        p.add_argument("--config", default=DEFAULT_CONFIG)
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.1f, min %.2f)" % (PAUSE, MIN_PAUSE))
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="pre-market setups from daily closes")
    common(p)
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                   help="v8 charts to fetch in stage 2, most traded first (%d; 0 = all)" % MAX_CONFIRM)
    p.add_argument("--at", help="cut the clock at this IST time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after, %s)" % DEFAULT_NO_ENTRIES_AFTER)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except ScanError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
