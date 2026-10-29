#!/usr/bin/env python3
"""Order-inflow screen for nse-orderbook. Standard library only (pdftotext is optional).

Reads the order wins NSE companies file as corporate announcements and works out each order's value:

    python3 orderbook.py fetch --from 2026-10-01 --to 2026-10-28   # pull filings, keep order/termination rows
    python3 orderbook.py parse                                     # read each PDF, parse the order value

Where the data comes from (public NSE endpoints, no login):
  - https://www.nseindia.com/api/corporate-announcements?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
    fetched in 15-day chunks and cached in nse/cache/orderbook/ann/<from>_<to>.json
  - the attached PDF of each filing, cached in nse/cache/orderbook/pdf/<seq_id>.pdf and read with
    `pdftotext -layout` into nse/cache/orderbook/txt/<seq_id>.txt (never downloaded twice)

Only category "Bagging/Receiving of orders/contracts" counts as an order and "Rescission/termination(s)"
as a termination; every other category is ignored. Parsed orders live in nse/state/orderbook.json, keyed
by seq_id, so running `parse` twice changes nothing.

Offline: --offline DIR reads ann/<from>_<to>.json, pdf/<seq_id>.pdf and txt/<seq_id>.txt from DIR.
This is research, not a buy signal. It never places orders. Not financial advice.
"""

import argparse
import datetime as _dt
import gzip
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

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

ANN_URL = ("https://www.nseindia.com/api/corporate-announcements?index=equities"
           "&from_date=%s&to_date=%s")
UA = "Mozilla/5.0 (nse-day-trader-skill)"
NSE_PAUSE = 0.25
CHUNK_DAYS = 15
CACHE_DIR = os.path.join("nse", "cache", "orderbook")
STATE = os.path.join("nse", "state", "orderbook.json")
ORDER_CAT = "Bagging/Receiving of orders/contracts"
TERM_CAT = "Rescission/termination(s)"
KINDS = {ORDER_CAT: "order", TERM_CAT: "termination"}
FX = {"USD": Decimal("84.0"), "EUR": Decimal("91.0")}
GST_DIVISOR = Decimal("1.18")
NO_PDFTOTEXT = "pdftotext not found: order values read from filing text only"
MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


class OrderbookError(Exception):
    pass


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, pause=NSE_PAUSE):
        self.pause = pause
        self._last = 0.0

    def get(self, url):
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
        except urllib.error.HTTPError as exc:
            raise OrderbookError("%s: HTTP %s" % (url, exc.code))
        except (urllib.error.URLError, OSError) as exc:
            raise OrderbookError("%s: %s" % (url, getattr(exc, "reason", exc)))
        finally:
            self._last = time.time()
        return data


_FETCHER = Fetcher()


def _write_atomic(path, data):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    os.replace(tmp, path)


def chunks(from_d, to_d, days=CHUNK_DAYS):
    """[(start, end)] covering from_d..to_d inclusive, each at most `days` long."""
    out = []
    a = from_d
    while a <= to_d:
        b = min(to_d, a + _dt.timedelta(days=days - 1))
        out.append((a, b))
        a = b + _dt.timedelta(days=1)
    return out


def _ddmmyyyy(d):
    return d.strftime("%d-%m-%Y")


def fetch_announcements(from_d, to_d, cache_dir, offline=None, fetcher=None):
    """Every announcement from_d..to_d (dates), in 15-day chunks. A chunk on disk is reused, except one
    that ends today or later (it may still grow). Offline reads DIR/ann/<from>_<to>.json only."""
    fetcher = fetcher or _FETCHER
    today = to_ist(_dt.datetime.now(_dt.timezone.utc)).date()
    rows = []
    for a, b in chunks(from_d, to_d):
        name = "%s_%s.json" % (_ddmmyyyy(a), _ddmmyyyy(b))
        if offline:
            path = os.path.join(offline, "ann", name)
            if not os.path.isfile(path):
                raise OrderbookError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                data = fh.read()
        else:
            path = os.path.join(cache_dir, "ann", name)
            if os.path.isfile(path) and b < today:
                with open(path, "rb") as fh:
                    data = fh.read()
            else:
                data = fetcher.get(ANN_URL % (_ddmmyyyy(a), _ddmmyyyy(b)))
                json.loads(data.decode("utf-8-sig"))  # a broken reply is never cached
                _write_atomic(path, data)
        got = json.loads(data.decode("utf-8-sig", "replace"))
        if isinstance(got, dict):
            got = got.get("data") or []
        rows.extend(got)
    return rows


def _iso_at(text):
    """'15-Nov-2024 13:31:09' -> '2024-11-15T13:31:09+05:30' ('' if unreadable)."""
    m = re.match(r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", text or "")
    if not m or m.group(2).upper() not in MONTHS:
        return ""
    return "%s-%02d-%02dT%02d:%s:%s+05:30" % (m.group(3), MONTHS[m.group(2).upper()], int(m.group(1)),
                                              int(m.group(4)), m.group(5), m.group(6) or "00")


def order_rows(raw):
    """Announcements -> order and termination rows, oldest first, each seq_id and each PDF once."""
    out, seqs, pdfs = [], set(), set()
    for r in sorted(raw or [], key=lambda x: (str(x.get("sort_date") or ""), str(x.get("seq_id") or ""))):
        kind = KINDS.get((r.get("desc") or "").strip())
        seq = str(r.get("seq_id") or "").strip()
        if not kind or not seq or seq in seqs:
            continue
        pdf = (r.get("attchmntFile") or "").strip()
        if pdf and pdf.rsplit("/", 1)[-1].lower() in pdfs:
            continue
        seqs.add(seq)
        if pdf:
            pdfs.add(pdf.rsplit("/", 1)[-1].lower())
        out.append({"seq_id": seq, "symbol": (r.get("symbol") or "").strip().upper(),
                    "name": html.unescape(r.get("sm_name") or "").strip(), "kind": kind,
                    "at": _iso_at(r.get("an_dt") or r.get("exchdisstime")),
                    "desc": html.unescape(r.get("attchmntText") or "").strip(), "pdf_url": pdf})
    return out


def have_pdftotext():
    return shutil.which("pdftotext")


def pdf_text(row, cache_dir, offline=None, fetcher=None):
    """The filing PDF as text (pdftotext -layout), cached by seq_id. None when there is no PDF, it cannot be
    read, or pdftotext is missing. Offline reads DIR/txt/<seq>.txt, else DIR/pdf/<seq>.pdf."""
    seq = row["seq_id"]
    base = offline or cache_dir
    txt = os.path.join(base, "txt", seq + ".txt")
    if os.path.isfile(txt):
        with open(txt, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    pdf = os.path.join(base, "pdf", seq + ".pdf")
    if not os.path.isfile(pdf):
        if offline or not (row.get("pdf_url") or "").lower().endswith(".pdf"):
            return None
        _write_atomic(pdf, (fetcher or _FETCHER).get(row["pdf_url"]))
    tool = have_pdftotext()
    if not tool:
        return None
    try:
        out = subprocess.run([tool, "-layout", pdf, "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=60).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return None
    if not offline:
        _write_atomic(txt, out)
    return out


# ---------------------------------------------------------------- order values

UNIT = {"crore": Decimal(10) ** 7, "crores": Decimal(10) ** 7, "cr": Decimal(10) ** 7, "cr.": Decimal(10) ** 7,
        "lakh": Decimal(10) ** 5, "lakhs": Decimal(10) ** 5, "lac": Decimal(10) ** 5, "lacs": Decimal(10) ** 5,
        "million": Decimal(10) ** 6, "mn": Decimal(10) ** 6, "billion": Decimal(10) ** 9, "bn": Decimal(10) ** 9}
CUR = r"(?P<cur>rs\.?|inr|₹|us\s?\$|usd|eur|€)"
NUM = r"(?P<a>\d[\d,]*(?:\.\d+)?)"
RANGE = r"(?:\s*(?:-|–|to)\s*(?:rs\.?|inr|₹)?\s*(?P<b>\d[\d,]*(?:\.\d+)?))?"
UNITS = r"(?:\s*(?P<u>crores?|cr\.?|lakhs?|lacs?|million|mn|billion|bn)\b)?"
AMOUNT = re.compile(CUR + r"\s*" + NUM + RANGE + UNITS, re.I)
SIZE_LINE = re.compile(r"(size|consideration)\s+of\s+the\s+order|broad\s+consideration|order\s+size\b|"
                       r"value\s+of\s+the\s+(order|contract)|contract\s+value\b|order\s+value\b", re.I)
LEGEND = re.compile(r"\b(up\s?to|above|below|less\s+than|more\s+than|exceeding)\s*$", re.I)
ORDER_WORDS = re.compile(r"\b(order|orders|contract|work\s*order|loa|value|worth|aggregat\w*|secured|"
                         r"bagged|received|awarded)\b", re.I)
NOT_ORDER = re.compile(r"(turnover|emd|earnest|bank\s+guarantee|net\s+worth|share\s+capital|paid[- ]up|"
                       r"penalty|gst\s+amounting|security\s+deposit|performance\s+guarantee)", re.I)
GST_IN = re.compile(r"^[^.;]{0,40}?(includ\w*|inclusive)\s+(of\s+)?(all\s+)?(gst|tax|taxes|duties)", re.I)
L1 = re.compile(r"(emerge\w*|declared|been\s+declared)\s+(as\s+)?(the\s+)?(lowest\s+bidder|L-?1)\b", re.I)
LOI = re.compile(r"(recei\w+|issu\w+|secur\w+|award\w*)\s+(of\s+)?(a\s+|the\s+)?(letter\s+of\s+intent|LoI)\b|"
                 r"(letter\s+of\s+intent|\bLoI\b)\s+(has\s+been|was|is)\s+(received|issued)", re.I)


def _num(s):
    try:
        return Decimal(s.replace(",", ""))
    except InvalidOperation:
        return None


def _candidates(text):
    flat = re.sub(r"\s+", " ", text or "")
    out = []
    for m in AMOUNT.finditer(flat):
        a = _num(m.group("a"))
        if a is None or a <= 0:
            continue
        before = flat[max(0, m.start() - 70):m.start()]
        if NOT_ORDER.search(before[-35:]) or LEGEND.search(before[-14:]):
            continue
        cur = m.group("cur").lower().replace(" ", "")
        unit = (m.group("u") or "").lower()
        mult = UNIT.get(unit, Decimal(1))
        if not unit and cur in ("usd", "us$", "eur", "€"):
            mult = Decimal(1)
        b = _num(m.group("b")) if m.group("b") else None
        flags = set()
        val = a
        if b is not None and b > a:
            val = (a + b) / 2
            flags.add("range")
        val = val * mult
        if cur in ("usd", "us$"):
            val, flags = val * FX["USD"], flags | {"fx"}
        elif cur in ("eur", "€"):
            val, flags = val * FX["EUR"], flags | {"fx"}
        if not unit and cur in ("rs", "rs.", "inr", "₹") and val < 1000:
            continue  # "Rs. 10" face value, not an order
        after = flat[m.end():m.end() + 60]
        if GST_IN.search(after):
            val, flags = val / GST_DIVISOR, flags | {"gst"}
        sentence = flat[max(0, m.start() - 160):m.end() + 40]
        prio = 3 if SIZE_LINE.search(flat[max(0, m.start() - 90):m.start()]) else (
            2 if ORDER_WORDS.search(sentence) else 0)
        out.append((prio, m.start(), val, flags, m.group(0)))
    gst_vals = {(c[2] * GST_DIVISOR).quantize(Decimal(1)) for c in out if "gst" in c[3]}
    return [(p, st, v / GST_DIVISOR, f | {"gst"}, r)
            if ("gst" not in f and v.quantize(Decimal(1)) in gst_vals) else (p, st, v, f, r)
            for (p, st, v, f, r) in out]


def _legend_band(text):
    """A classified order ('Ultra-mega*' against an 'Order Classification' table of ranges), as
    (midpoint rupees, raw) or None. Open-ended top classes ('> 15,000') count at their floor."""
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if not re.match(r"^\s*Classification\s", line) or i + 1 >= len(lines):
            continue
        names = re.split(r"\s{2,}", line.strip())[1:]
        vals = re.split(r"\s{2,}", re.sub(r"^\s*Value in\s+\S+\s*Cr\s*", "", lines[i + 1]).strip())
        if not names or len(names) != len(vals):
            continue
        for name, rng in zip(names, vals):
            if re.search(r"(?<![\w-])" + re.escape(name) + r"\*", text, re.I):
                nums = [_num(x) for x in re.findall(r"\d[\d,]*", rng)]
                nums = [x for x in nums if x]
                if not nums:
                    return None
                mid = (nums[0] + nums[-1]) / 2 if len(nums) > 1 else nums[0]
                return mid * UNIT["crore"], "%s* (%s cr)" % (name, rng)
    return None


def parse_value(text):
    """The order's value in rupees from filing text: the Annexure 'size' line first, then an amount in a
    sentence about the order; never just the biggest number. GST-inclusive values are divided by 1.18."""
    flags = set()
    if text and L1.search(text):
        flags.add("l1")
    if text and LOI.search(text):
        flags.add("loi")
    cands = [c for c in _candidates(text) if c[0] > 0]
    if not cands:
        band = _legend_band(text)
        if band:
            return {"value_inr": band[0].quantize(Decimal(1)), "basis": "band",
                    "flags": sorted(flags | {"range"}), "raw": band[1]}
        return {"value_inr": None, "basis": "none", "flags": sorted(flags), "raw": ""}
    top = max(c[0] for c in cands)
    prio, _, val, f, raw = min((c for c in cands if c[0] == top), key=lambda c: c[1])
    flags |= f
    return {"value_inr": val.quantize(Decimal(1)), "basis": "band" if "range" in f else "text",
            "flags": sorted(flags), "raw": raw}


def parse_order(row, text):
    """row + its value: the announcement description first, then the PDF text."""
    got = parse_value(row.get("desc") or "")
    if got["value_inr"] is None and text:
        got = parse_value(text)
        if got["basis"] == "text":
            got["basis"] = "pdf"
    out = dict(row)
    out.update(got)
    out["value_inr"] = str(got["value_inr"]) if got["value_inr"] is not None else None
    return out


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, doc):
    _write_atomic(path, json.dumps(doc, indent=1, sort_keys=True))

# ---------------------------------------------------------------- prices and revenue (Yahoo, cached)

CHART = "https://query2.finance.yahoo.com/v8/finance/chart/%s?period1=%d&period2=%d&interval=1d"
SERIES = ("https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/%s"
          "?type=annualTotalRevenue,quarterlyTotalRevenue&period1=1483228800&period2=%d")
_YF = Fetcher(pause=0.15)


def _cached_json(url, path, offline=None, max_age_days=None):
    if offline:
        p = os.path.join(offline, os.path.basename(os.path.dirname(path)), os.path.basename(path))
        if not os.path.isfile(p):
            return None
        with open(p, "rb") as fh:
            return json.loads(fh.read().decode("utf-8", "replace"))
    if os.path.isfile(path):
        age = (time.time() - os.path.getmtime(path)) / 86400
        if max_age_days is None or age < max_age_days:
            with open(path, "rb") as fh:
                return json.loads(fh.read().decode("utf-8", "replace"))
    try:
        data = _YF.get(url)
    except OrderbookError:
        return None
    try:
        doc = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return None
    _write_atomic(path, data)
    return doc


def daily_bars(sym, cache_dir, offline=None, start=_dt.date(2023, 6, 1), max_age_days=0.5, ysym=None):
    """[{date, open, close, volume}] oldest first, NSE daily bars from Yahoo. [] if Yahoo has none."""
    p1 = int(_dt.datetime(start.year, start.month, start.day, tzinfo=_dt.timezone.utc).timestamp())
    p2 = int(time.time()) + 86400
    doc = _cached_json(CHART % (ysym or yahoo_quote(sym), p1, p2), os.path.join(cache_dir, "px", sym + ".json"),
                       offline, max_age_days)
    try:
        res = doc["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        ts = res["timestamp"]
    except (TypeError, KeyError, IndexError):
        return []
    out = []
    for i, t in enumerate(ts):
        o, c, v = q["open"][i], q["close"][i], q["volume"][i]
        if o is None or c is None or not v:
            continue
        out.append({"date": to_ist(_dt.datetime.fromtimestamp(t, _dt.timezone.utc)).date(),
                    "open": Decimal(str(o)), "close": Decimal(str(c)), "volume": int(v)})
    return out


def revenues(sym, cache_dir, offline=None, max_age_days=7):
    """(annual, quarterly): lists of (period_end date, revenue Decimal) oldest first."""
    doc = _cached_json(SERIES % (yahoo_quote(sym), int(time.time()) + 86400),
                       os.path.join(cache_dir, "rev", sym + ".json"), offline, max_age_days)
    out = {"annualTotalRevenue": {}, "quarterlyTotalRevenue": {}}
    try:
        results = doc["timeseries"]["result"]
    except (TypeError, KeyError):
        return [], []
    for r in results or []:
        for key in out:
            for e in r.get(key) or []:
                if e and (e.get("reportedValue") or {}).get("raw") is not None:
                    out[key][_dt.date.fromisoformat(e["asOfDate"])] = Decimal(str(e["reportedValue"]["raw"]))
    return (sorted(out["annualTotalRevenue"].items()), sorted(out["quarterlyTotalRevenue"].items()))


def revenue_at(annual, d, lag_days=60):
    """(revenue, growth) of the latest fiscal year whose end + lag_days <= d; growth vs the year before."""
    known = [(e, v) for e, v in annual if e + _dt.timedelta(days=lag_days) <= d and v > 0]
    if not known:
        return None, None
    rev = known[-1][1]
    growth = (rev / known[-2][1] - 1) if len(known) > 1 else None
    return rev, growth


def quarter_ok(quarterly, d, lag_days=45):
    """Latest known quarter >= same quarter a year earlier (True/False), or None if unknown."""
    known = [(e, v) for e, v in quarterly if e + _dt.timedelta(days=lag_days) <= d]
    if not known:
        return None
    end, v = known[-1]
    prev = [x for e, x in known if abs((end - e).days - 365) <= 10]
    return (v >= prev[-1]) if prev else None


def bars_upto(bars, d):
    return [b for b in bars if b["date"] <= d]


def median_value_cr(bars, d, days=60):
    xs = sorted(b["close"] * b["volume"] for b in bars_upto(bars, d)[-days:])
    if not xs:
        return None
    return xs[len(xs) // 2] / UNIT["crore"]

# ---------------------------------------------------------------- the screen

DEFAULTS = {"window_days": 90, "history_days": 270, "min_intensity": 0.10, "min_accel": 1.25, "accel_cap": 3,
            "min_rev_growth": 0.10, "max_termination_share": 0.25, "revenue_lag_days": 60, "min_price": 50,
            "min_median_value_cr": 10, "liquidity_days": 60, "top_n": 10, "hold_months": 3,
            "capital": 1000000,
            "delivery_charges": {"brokerage": 0, "stt_pct": 0.1, "txn_pct": 0.00307, "sebi_per_crore": 10,
                                 "stamp_buy_pct": 0.015, "gst_pct": 18, "dp_per_sell": 15.93}}


def load_cfg(path="nse/config.json"):
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(path, encoding="utf-8") as fh:
            user = (json.load(fh) or {}).get("orderbook") or {}
    except (OSError, ValueError):
        user = {}
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def _day(o):
    return _dt.date.fromisoformat((o.get("at") or "")[:10])


def signal(orders, d, cfg):
    """Order inflow for one company as known on date d (filings after d are ignored)."""
    w = cfg["window_days"]
    lo = d - _dt.timedelta(days=w)
    hist_lo = lo - _dt.timedelta(days=cfg["history_days"])
    gross = terms = prior = Decimal(0)
    n = n_valued = 0
    ids = []
    for o in orders:
        day = _day(o)
        if day > d or day <= hist_lo:
            continue
        v = Decimal(o["value_inr"]) if o.get("value_inr") else None
        if day > lo:
            if o["kind"] == "order":
                n += 1
                if v:
                    n_valued += 1
                    gross += v
                    ids.append(o["seq_id"])
            elif v:
                terms += v
        elif v and o["kind"] == "order":
            prior += v
    inflow = gross - terms
    prior_avg = prior / (Decimal(cfg["history_days"]) / w)
    return {"inflow": inflow, "gross": gross, "terminations": terms, "prior_avg": prior_avg,
            "n_orders": n, "n_valued": n_valued, "seq_ids": ids}


def screen(d, by_symbol, data, cfg, live=False, asm=()):
    """(picks, rejected) as of date d. by_symbol: {sym: [orders]}; data(sym) -> (bars, annual, quarterly)."""
    picks, rej = [], {}

    def no(reason, sym):
        rej.setdefault(reason, []).append(sym)

    for sym, orders in sorted(by_symbol.items()):
        s = signal(orders, d, cfg)
        if not s["n_orders"]:
            continue
        if not s["n_valued"]:
            no("no_value", sym)
            continue
        if s["gross"] > 0 and s["terminations"] / s["gross"] >= Decimal(str(cfg["max_termination_share"])):
            no("terminations", sym)
            continue
        if sym in asm:
            no("asm", sym)
            continue
        bars, annual, quarterly = data(sym)
        past = bars_upto(bars, d)
        if not past:
            no("no_price_data", sym)
            continue
        if past[-1]["close"] < Decimal(str(cfg["min_price"])):
            no("price", sym)
            continue
        mv = median_value_cr(bars, d, cfg["liquidity_days"])
        if mv is None or mv < Decimal(str(cfg["min_median_value_cr"])):
            no("illiquid", sym)
            continue
        rev, growth = revenue_at(annual, d, cfg["revenue_lag_days"])
        if not rev:
            no("no_revenue", sym)
            continue
        intensity = s["inflow"] / rev
        if intensity < Decimal(str(cfg["min_intensity"])):
            no("low_intensity", sym)
            continue
        accel = (s["inflow"] / s["prior_avg"]) if s["prior_avg"] > 0 else Decimal(cfg["accel_cap"])
        if accel < Decimal(str(cfg["min_accel"])):
            no("no_accel", sym)
            continue
        if growth is None or growth < Decimal(str(cfg["min_rev_growth"])):
            no("rev_not_growing", sym)
            continue
        if live and quarter_ok(quarterly, d) is False:
            no("qtr_falling", sym)
            continue
        score = intensity * min(accel, Decimal(cfg["accel_cap"]))
        picks.append({"symbol": sym, "score": score, "intensity": intensity, "accel": accel, "rev": rev,
                      "rev_growth": growth, "inflow": s["inflow"], "n_orders": s["n_orders"],
                      "median_value_cr": mv, "close": past[-1]["close"], "seq_ids": s["seq_ids"]})
    picks.sort(key=lambda p: (-p["score"], p["symbol"]))
    return picks[:cfg["top_n"]], rej


def delivery_charges(buy_value, sell_value, sells=1, cfg=None):
    """Equity delivery (CNC) charges in rupees, rounded to paisa per component."""
    c = (cfg or DEFAULTS)["delivery_charges"]
    b, s = Decimal(str(buy_value)), Decimal(str(sell_value))
    pct = lambda x, p: (x * Decimal(str(p)) / 100).quantize(Decimal("0.01"))
    brok = Decimal(str(c["brokerage"]))
    stt = pct(b, c["stt_pct"]) + pct(s, c["stt_pct"])
    txn = pct(b, c["txn_pct"]) + pct(s, c["txn_pct"])
    sebi = ((b + s) * Decimal(str(c["sebi_per_crore"])) / UNIT["crore"]).quantize(Decimal("0.01"))
    stamp = pct(b, c["stamp_buy_pct"])
    gst = pct(brok + txn + sebi, c["gst_pct"])
    dp = (Decimal(str(c["dp_per_sell"])) * sells) if s > 0 else Decimal(0)
    return brok + stt + txn + sebi + stamp + gst + dp

# ---------------------------------------------------------------- backtest

def month_ends(start, end, cal):
    """Last trading day (from calendar `cal`, sorted dates) of each month in start..end."""
    out = {}
    for d in cal:
        if start <= d <= end:
            out[(d.year, d.month)] = d
    return [out[k] for k in sorted(out)]


def backtest(start, end, by_symbol, data, cfg, bench=None):
    """Monthly signals, enter next open, hold hold_months, equal weight over active picks, delivery charges.
    data(sym) -> (bars, annual, quarterly). bench: [{date, close}] for the trading calendar and comparison."""
    cal = [b["date"] for b in (bench or [])]
    ends = month_ends(start, end, cal)
    px = {}

    def bars_of(sym):
        if sym not in px:
            px[sym] = {b["date"]: b for b in data(sym)[0]}
        return px[sym]

    cash = Decimal(cfg["capital"])
    pos = {}          # sym -> shares (Decimal, whole)
    active = []       # (exit_index_month, sym)
    trades, months, charges = [], [], Decimal(0)
    equity = []
    days = [d for d in cal if start <= d <= end]
    sig_on = {d: i for i, d in enumerate(ends)}
    pending = None
    for d in days:
        if pending is not None:
            i, picks = pending
            pending = None
            active = [(x, s) for (x, s) in active if x > i] + [(i + cfg["hold_months"], p["symbol"]) for p in picks]
            want = sorted({s for _, s in active if d in bars_of(s)})
            value = cash + sum(n * bars_of(s)[d]["open"] for s, n in pos.items() if d in bars_of(s))
            target = (value / len(want)) if want else Decimal(0)
            for s in list(pos):
                if s not in want and d in bars_of(s):
                    sell = pos.pop(s) * bars_of(s)[d]["open"]
                    ch = delivery_charges(0, sell, 1, cfg)
                    cash += sell - ch
                    charges += ch
                    trades.append({"date": str(d), "symbol": s, "side": "SELL", "value": str(sell.quantize(Decimal(1)))})
            for s in want:
                o = bars_of(s)[d]["open"]
                have = pos.get(s, 0) * o
                gap = target - have
                n = int(abs(gap) / o)
                if n == 0:
                    continue
                v = n * o
                if gap > 0:
                    ch = delivery_charges(v, 0, 0, cfg)
                    if v + ch > cash:
                        n = int((cash - ch) / o) if cash > ch else 0
                        v = n * o
                        if n <= 0:
                            continue
                    cash -= v + ch
                    pos[s] = pos.get(s, 0) + n
                    side = "BUY"
                else:
                    ch = delivery_charges(0, v, 1, cfg)
                    cash += v - ch
                    pos[s] -= n
                    side = "SELL"
                charges += ch
                trades.append({"date": str(d), "symbol": s, "side": side, "value": str(v.quantize(Decimal(1)))})
        mark = cash
        for s, n in pos.items():
            known = [b for k, b in bars_of(s).items() if k <= d]
            if known:
                mark += n * max(known, key=lambda b: b["date"])["close"]
        equity.append((d, mark))
        if d in sig_on:
            picks, rej = screen(d, by_symbol, data, cfg)
            months.append({"date": str(d), "picks": [p["symbol"] for p in picks],
                           "rejected": {k: len(v) for k, v in rej.items()}})
            pending = (sig_on[d], picks)
    return {"equity": equity, "trades": trades, "months": months, "charges": charges,
            "stats": stats(equity, bench or [], charges, trades, months)}


def _dd(series):
    peak, worst = None, Decimal(0)
    for _, v in series:
        peak = v if peak is None or v > peak else peak
        worst = min(worst, v / peak - 1)
    return worst


def stats(equity, bench, charges, trades, months):
    if not equity:
        return {}
    first, last = equity[0][1], equity[-1][1]
    years = Decimal((equity[-1][0] - equity[0][0]).days) / Decimal("365.25")
    total = last / first - 1
    cagr = (float(last / first) ** (1 / float(years)) - 1) if years > 0 else None
    bmap = {b["date"]: b["close"] for b in bench}
    bser = [(d, bmap[d]) for d, _ in equity if d in bmap]
    btotal = (bser[-1][1] / bser[0][1] - 1) if len(bser) > 1 else None
    bcagr = (float(bser[-1][1] / bser[0][1]) ** (1 / float(years)) - 1) if btotal is not None and years > 0 else None
    beat = n_m = 0
    mon = {}
    for d, v in equity:
        mon[(d.year, d.month)] = (d, v)
    keys = sorted(mon)
    for a, b in zip(keys, keys[1:]):
        da, va = mon[a]
        db, vb = mon[b]
        if da in bmap and db in bmap:
            n_m += 1
            beat += (vb / va) > (bmap[db] / bmap[da])
    held = [len(m["picks"]) for m in months]
    return {"start": str(equity[0][0]), "end": str(equity[-1][0]), "final": last, "total_return": total,
            "cagr": cagr, "max_dd": _dd(equity), "bench_total": btotal, "bench_cagr": bcagr,
            "bench_max_dd": _dd(bser) if bser else None, "months_beat": beat, "months": n_m,
            "trades": len(trades), "charges": charges,
            "avg_new_picks": (sum(held) / len(held)) if held else 0,
            "empty_months": sum(1 for h in held if h == 0)}

# ---------------------------------------------------------------- commands

def _rows(args, from_d, to_d):
    return order_rows(fetch_announcements(from_d, to_d, args.cache, args.offline))


def _parse_rows(rows, args, state, only=None, say=print):
    """Parse each new filing (PDF read only when the description has no value). Updates state in place."""
    tool = have_pdftotext()
    if not tool and not args.offline:
        say(NO_PDFTOTEXT)
    todo = [r for r in rows if r["seq_id"] not in state and (only is None or r["symbol"] in only)]
    for i, r in enumerate(todo, 1):
        text = None
        if parse_value(r.get("desc") or "")["value_inr"] is None:
            try:
                text = pdf_text(r, args.cache, args.offline)
            except OrderbookError as exc:
                say("  skipped PDF %s (%s)" % (r["seq_id"], exc))
        state[r["seq_id"]] = parse_order(r, text)
        if i % 100 == 0:
            say("  parsed %d of %d filings" % (i, len(todo)))
            save_state(args.state, state)
    return state


def _by_symbol(state):
    out = {}
    for o in state.values():
        if o.get("at"):
            out.setdefault(o["symbol"], []).append(o)
    return out


def _data(args):
    memo = {}

    def data(sym):
        if sym not in memo:
            a, q = revenues(sym, args.cache, args.offline)
            memo[sym] = (daily_bars(sym, args.cache, args.offline), a, q)
        return memo[sym]
    return data


def _cr(x):
    return "n/a" if x is None else "%s cr" % format((Decimal(x) / UNIT["crore"]).quantize(Decimal("0.1")), ",")


def _pct(x):
    return "n/a" if x is None else "%.1f%%" % (float(x) * 100)


def print_picks(picks, rej, d):
    print("Order-inflow screen as of %s (research list, not a buy signal):" % d)
    if not picks:
        print("  no company passes every filter today.")
    for i, p in enumerate(picks, 1):
        print("  %2d. %-12s score %.2f  orders 90d %s = %s of revenue, %.1fx its usual pace,"
              " revenue growth %s, traded %s/day" % (i, p["symbol"], p["score"], _cr(p["inflow"]),
                                                       _pct(p["intensity"]), p["accel"], _pct(p["rev_growth"]),
                                                       _cr(p["median_value_cr"] * UNIT["crore"])))
    if rej:
        print("  left out: " + ", ".join("%s %d" % (k, len(v)) for k, v in sorted(rej.items())))


def cmd_fetch(args):
    rows = _rows(args, _dt.date.fromisoformat(args.from_d), _dt.date.fromisoformat(args.to_d))
    state = _parse_rows(rows, args, load_state(args.state)) if args.parse else load_state(args.state)
    save_state(args.state, state)
    print("%d order/termination filings in range; %d parsed in state" % (len(rows), len(state)))
    return 0


def cmd_daily(args):
    cfg = load_cfg(args.config)
    today = parse_now(args.now).date() if args.now else to_ist(_dt.datetime.now(_dt.timezone.utc)).date()
    state = load_state(args.state)
    start = today - _dt.timedelta(days=cfg["window_days"] + cfg["history_days"] + 5)
    rows = _rows(args, start, today)
    state = _parse_rows(rows, args, state)
    save_state(args.state, state)
    fresh = [state[r["seq_id"]] for r in rows if r["at"][:10] == str(today) and r["kind"] == "order"]
    print("New order filings today (%s): %d" % (today, len(fresh)))
    for o in fresh:
        print("  %-12s %-10s %s %s" % (o["symbol"], _cr(o["value_inr"]), o["basis"], ",".join(o["flags"])))
    picks, rej = screen(today, _by_symbol(state), _data(args), cfg, live=True)
    print_picks(picks, rej, today)
    if args.write:
        doc = {"date": str(today), "created_by": "nse-orderbook",
               "picks": [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in p.items()} for p in picks]}
        _write_atomic(os.path.join("nse", "orderbook-picks.json"), json.dumps(doc, indent=1))
        track = load_state(TRACK)
        for p in picks:
            track.setdefault("%s|%s" % (today, p["symbol"]), {"date": str(today), "symbol": p["symbol"],
                                                               "price": str(p["close"])})
        save_state(TRACK, track)
    return 0


def cmd_track(args):
    track = load_state(TRACK)
    if not track:
        print("No picks logged yet: run `orderbook.py daily --write` each day.")
        return 0
    data = _data(args)
    rets = []
    for k in sorted(track):
        t = track[k]
        bars = data(t["symbol"])[0]
        if not bars:
            continue
        r = bars[-1]["close"] / Decimal(t["price"]) - 1
        rets.append(r)
        print("  %s %-12s picked at %s, now %s (%s)" % (t["date"], t["symbol"], t["price"], bars[-1]["close"], _pct(r)))
    if rets:
        rets.sort()
        print("Median since pick %s over %d picks; %d up." % (_pct(rets[len(rets) // 2]), len(rets),
                                                              sum(1 for r in rets if r > 0)))
    return 0


def cmd_backtest(args):
    cfg = load_cfg(args.config)
    start, end = _dt.date.fromisoformat(args.from_d), _dt.date.fromisoformat(args.to_d)
    lookback = start - _dt.timedelta(days=cfg["window_days"] + cfg["history_days"] + 5)
    print("Fetching order filings %s to %s ..." % (lookback, end))
    rows = _rows(args, lookback, end)
    syms = sorted({r["symbol"] for r in rows})
    print("%d filings from %d companies. Reading prices to drop illiquid names first ..." % (len(rows), len(syms)))
    data = _data(args)
    liquid = set()
    for s in syms:
        bars = data(s)[0]
        if any((median_value_cr(bars, b["date"], cfg["liquidity_days"]) or 0) >= cfg["min_median_value_cr"]
               for b in bars[::20] if start <= b["date"] <= end):
            liquid.add(s)
    print("%d companies were ever liquid enough; reading their filings' PDFs ..." % len(liquid))
    state = _parse_rows(rows, args, load_state(args.state), only=liquid)
    save_state(args.state, state)
    book = {s: o for s, o in _by_symbol(state).items() if s in liquid}
    bench = daily_bars("^NSEI", args.cache, args.offline, ysym="^NSEI")
    res = backtest(start, end, book, data, cfg, bench)
    used = [o for o in state.values() if o["symbol"] in liquid and o["kind"] == "order"]
    cov = {b: sum(1 for o in used if o["basis"] == b) for b in ("text", "pdf", "band", "none")}
    res["stats"]["coverage"] = cov
    report = render_backtest(res, cfg)
    print(report)
    out = os.path.join("nse", "backtest", "orderbook-%s_%s" % (start, end))
    _write_atomic(out + ".md", report)
    _write_atomic(out + ".csv", "date,equity\n" + "".join("%s,%s\n" % (d, v.quantize(Decimal(1)))
                                                          for d, v in res["equity"]))
    _write_atomic(out + ".json", json.dumps({"months": res["months"], "trades": res["trades"]}, indent=1))
    print("Saved %s.md, .csv and .json" % out)
    return 0

TRACK = os.path.join("nse", "state", "orderbook-track.json")
CAVEATS = """Read this before trusting the number:
  - Past results, simulated. Not financial advice, and not a buy list.
  - Universe = companies that filed orders and that Yahoo still has prices for (survivorship bias).
  - Order values are read from PDFs by pattern; some are wrong or missing (coverage above).
  - Revenue is assumed known 60 days after each fiscal year ends; Yahoo's figures can be restated.
  - Fills at the next day's open with no slippage; delivery charges included, taxes on gains not.
  - Two years and a few dozen trades is a small sample: luck can explain all of it."""


def ascii_chart(series, width=60, height=12, label="strategy"):
    if len(series) < 2:
        return ""
    step = max(1, len(series) // width)
    pts = [v for _, v in series[::step]]
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or Decimal(1)
    rows = []
    for r in range(height, -1, -1):
        level = lo + span * r / height
        line = "".join("*" if abs(p - level) <= span / height / 2 else " " for p in pts)
        rows.append("%12s |%s" % (format(level.quantize(Decimal(1)), ","), line))
    return "\n".join(rows) + "\n%12s  %s ... %s (%s)" % ("", series[0][0], series[-1][0], label)


def render_backtest(res, cfg):
    s = res["stats"]
    cov = s.get("coverage", {})
    total_orders = sum(cov.values()) or 1
    valued = total_orders - cov.get("none", 0)
    lines = ["# nse-orderbook backtest, %s to %s" % (s.get("start"), s.get("end")), "",
             "Rules: monthly signal on the last trading day; orders in the last %d days at least %d%% of "
             "annual revenue and at least %.2fx the company's usual pace; revenue growing at least %d%%; "
             "close at least Rs %s and at least Rs %s cr traded a day; top %d; each pick held %d months; "
             "equal weight; enter at the next open." % (
                 cfg["window_days"], cfg["min_intensity"] * 100, cfg["min_accel"], cfg["min_rev_growth"] * 100,
                 cfg["min_price"], cfg["min_median_value_cr"], cfg["top_n"], cfg["hold_months"]), "",
             "| | Strategy | Nifty 50 |", "|---|---|---|",
             "| Start capital | Rs %s | |" % format(int(cfg["capital"]), ","),
             "| End value | Rs %s | |" % format(int(s["final"]), ","),
             "| Total return | %s | %s |" % (_pct(s["total_return"]), _pct(s["bench_total"])),
             "| CAGR | %s | %s |" % (_pct(s["cagr"]), _pct(s["bench_cagr"])),
             "| Max drawdown | %s | %s |" % (_pct(s["max_dd"]), _pct(s["bench_max_dd"])),
             "| Months beating Nifty | %d of %d | |" % (s["months_beat"], s["months"]),
             "| Trades | %d | |" % s["trades"],
             "| Charges paid | Rs %s | |" % format(int(s["charges"]), ","),
             "| Months with no pick | %d | |" % s["empty_months"], "",
             "Order values found for %d of %d filings (%s from text, %s from PDF, %s bands)." % (
                 valued, total_orders, cov.get("text", 0), cov.get("pdf", 0), cov.get("band", 0)), "",
             "## Picks by month", ""]
    for m in res["months"]:
        lines.append("- %s: %s" % (m["date"], ", ".join(m["picks"]) or "none"))
    lines += ["", "## Equity curve", "", "```", ascii_chart(res["equity"]), "```", "", CAVEATS, ""]
    return "\n".join(lines)


def _parser():
    ap = argparse.ArgumentParser(description="NSE order-win filings, an order-inflow screen and its backtest.")
    sub = ap.add_subparsers(dest="cmd")
    f = sub.add_parser("fetch", help="pull order filings for a date range")
    f.add_argument("--from", dest="from_d", required=True)
    f.add_argument("--to", dest="to_d", required=True)
    f.add_argument("--parse", action="store_true", help="also read PDFs and parse order values")
    d = sub.add_parser("daily", help="today's new orders and the ranked list (run once a day)")
    d.add_argument("--now", help="pin the date, YYYY-MM-DD HH:MM IST")
    d.add_argument("--write", action="store_true", help="save nse/orderbook-picks.json and log picks")
    t = sub.add_parser("track", help="how logged picks have done since")
    b = sub.add_parser("backtest", help="replay the screen month by month")
    b.add_argument("--from", dest="from_d", default="2024-11-01")
    b.add_argument("--to", dest="to_d", default=None)
    for x in (f, d, t, b):
        x.add_argument("--offline", metavar="DIR")
        x.add_argument("--cache", default=CACHE_DIR)
        x.add_argument("--state", default=STATE)
        x.add_argument("--config", default="nse/config.json")
    return ap


def _run(args):
    if args.cmd == "backtest" and not args.to_d:
        args.to_d = str(to_ist(_dt.datetime.now(_dt.timezone.utc)).date())
    return {"fetch": cmd_fetch, "daily": cmd_daily, "track": cmd_track, "backtest": cmd_backtest}[args.cmd](args)


def main(argv=None):
    ap = _parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    try:
        return _run(args)
    except (OrderbookError, ValueError, OSError) as exc:
        print("orderbook: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
