#!/usr/bin/env python3
"""Quarterly results reader for nse-analyst. Standard library only.

Pulls a company's reported quarterly numbers from Yahoo Finance's fundamentals
timeseries (unofficial: check the company's own NSE filing before relying on a
number) and lays out what changed, quarter by quarter: revenue, gross, operating
and net margins, net income, diluted EPS, and growth against the same quarter a
year earlier, plus the trailing four quarters. It also finds the company's
latest results filing in NSE's Financial_Results RSS.

    python3 results.py RELIANCE
    python3 results.py HDFCBANK --quarters 8
    python3 results.py RELIANCE --json
    python3 results.py RELIANCE --offline tests/fixtures/nse-analyst

Sources:
  https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/<SYMBOL>.NS
  https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml

Rules: only 3-month periods count; null entries are ignored; the same quarter
reported twice keeps the last. Newest quarter first. Money is in rupees crore,
EPS in rupees. Fiscal labels follow the Indian year (April to March): the
quarter ending 2026-06-30 is "Q1 FY27". Growth compares the quarter about a year
earlier (365 days, 10 days either way); when that quarter is missing the answer
is n/a, never 0. A margin with a missing part is n/a. The trailing four quarters
are summed only when the latest four are all present and back to back.

Company names for the RSS match come from the universe file (symbol -> name,
default nse/universe.json, or universe.json in the --offline folder), matched by
exact normalised name. Offline: --offline DIR reads timeseries-<SYMBOL>.json,
Financial_Results.xml and universe.json from DIR.
"""

import argparse
import datetime as _dt
import gzip
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

UA = "Mozilla/5.0 (nse-day-trader-skill)"
NSE_PAUSE = 0.25
TIMESERIES = ("https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/%s"
              "?type=%s&period1=%d&period2=%d")
RESULTS_RSS = "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml"
TYPES = ("quarterlyTotalRevenue", "quarterlyGrossProfit", "quarterlyOperatingIncome",
         "quarterlyNetIncome", "quarterlyDilutedEPS")
REV, GP, OI, NI, EPS = TYPES
PERIOD1 = 1640995200  # 2022-01-01
SOURCE = "Yahoo Finance fundamentals (unofficial; check the company's NSE filing)"


class ResultsError(Exception):
    pass


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=NSE_PAUSE):
        self.offline, self.pause, self._last = offline, pause, 0.0

    def get(self, url, name):
        """Bytes of a URL, or of DIR/name when offline. A missing offline file is a ResultsError."""
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise ResultsError("offline: no file %s" % path)
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
            raise ResultsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise ResultsError("could not reach %s: %s" % (url, exc.reason))


def series_url(sym, now_ts=None):
    return TIMESERIES % (yahoo_quote(sym), ",".join(TYPES), PERIOD1, int(now_ts or time.time()))


def parse_series(doc):
    """Yahoo timeseries JSON -> {type: [(asOfDate, value)]}, oldest first. Only 3M periods; nulls dropped;
    the same date twice keeps the last."""
    out = {}
    for res in ((doc or {}).get("timeseries") or {}).get("result") or []:
        types = (res.get("meta") or {}).get("type") or []
        for t in types:
            by_date = {}
            for e in res.get(t) or []:
                if not e or e.get("periodType") != "3M" or not e.get("asOfDate"):
                    continue
                raw = (e.get("reportedValue") or {}).get("raw")
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    by_date[e["asOfDate"]] = float(raw)
            out[t] = sorted(by_date.items())
    return out


def fetch_series(sym, types=TYPES, fetcher=None):
    """-> {type: [(date, value)]} for the wanted types (an empty list for a type Yahoo has none of)."""
    fetcher = fetcher or Fetcher()
    try:
        data = fetcher.get(series_url(sym), "timeseries-%s.json" % str(sym).upper())
        doc = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise ResultsError("Yahoo answered with something that is not JSON: %s" % exc)
    parsed = parse_series(doc)
    return {t: parsed.get(t, []) for t in types}


# ---------------------------------------------------------------- names and filings

def normalise_name(name):
    """Company name -> comparison key: lowercase, '&' as 'and', no punctuation, no trailing Limited/Ltd."""
    s = str(name or "").lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"\s(?:limited|ltd)$", "", s)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    for c in el:
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def _pub(text):
    try:
        return _dt.datetime.strptime(text.strip(), "%d-%b-%Y %H:%M:%S")
    except ValueError:
        return _dt.datetime.min


def _field(desc, key):
    m = re.search(r"%s\s*:\s*([^|]*)" % re.escape(key), desc)
    return m.group(1).strip() if m else ""


def latest_filing(xml_bytes, company_name):
    """The newest quarterly results item for a company in Financial_Results.xml (exact normalised name),
    or None. -> {title, link, relating_to, consolidated, period_ended, published}."""
    want = normalise_name(company_name)
    if not want:
        return None
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ResultsError("Financial_Results.xml is not valid XML: %s" % exc)
    best = None
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        title = _child(item, "title")
        if normalise_name(title) != want:
            continue
        desc = _child(item, "description")
        if _field(desc, "PERIOD") != "Quarterly":
            continue
        cand = {"title": " ".join(title.split()), "link": _child(item, "link"),
                "relating_to": _field(desc, "RELATING TO"),
                "consolidated": _field(desc, "CONSOLIDATED/NON-CONSOLIDATED"),
                "period_ended": _field(desc, "PERIOD ENDED"), "published": _child(item, "pubDate")}
        if best is None or _pub(cand["published"]) > _pub(best["published"]):
            best = cand
    return best


def load_name(sym, universe=None, offline=None):
    path = universe or (os.path.join(offline, "universe.json") if offline else "nse/universe.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return ((json.load(fh).get("names") or {}).get(str(sym).upper()))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- numbers

def _date(s):
    return _dt.date.fromisoformat(s)


def fiscal_label(end):
    """Indian fiscal quarter: 2026-06-30 -> 'Q1 FY27' (Jun=Q1, Sep=Q2, Dec=Q3, Mar=Q4; FY = year of the March)."""
    d = _date(end)
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(d.month)
    if q is None:
        q = ((d.month - 4) % 12) // 3 + 1
    fy = d.year + 1 if d.month >= 4 else d.year
    return "Q%d FY%02d" % (q, fy % 100)


def _yoy_match(series_, end, days=365, slack=10):
    target = _date(end) - _dt.timedelta(days=days)
    best = None
    for e in series_:
        gap = abs((_date(e) - target).days)
        if gap <= slack and (best is None or gap < best[0]):
            best = (gap, e)
    return series_[best[1]] if best else None


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def analyse(series, quarters=6):
    """Every number in the report, as a dict. series is {type: [(date, value)]} from fetch_series."""
    s = {t: dict(series.get(t) or []) for t in TYPES}
    if not s[REV] and not s[NI]:
        raise ResultsError("no quarterly results")
    all_ends = sorted(set(s[REV]) | set(s[NI]), reverse=True)
    rows = []
    for end in all_ends[:quarters]:
        r, gp, oi, ni, eps = (s[t].get(end) for t in (REV, GP, OI, NI, EPS))
        rows.append({
            "end": end, "fiscal": fiscal_label(end),
            "revenue": r, "gross_profit": gp, "operating_income": oi, "net_income": ni,
            "eps_diluted": round(eps, 2) if eps is not None else None,
            "gross_margin": margin(gp, r), "operating_margin": margin(oi, r), "net_margin": margin(ni, r),
            "revenue_yoy": pct(r, _yoy_match(s[REV], end)),
            "net_income_yoy": pct(ni, _yoy_match(s[NI], end)),
            "eps_yoy": pct(eps, _yoy_match(s[EPS], end)),
        })
    ttm = {"revenue": None, "net_income": None, "eps_diluted": None}
    last4 = all_ends[:4]
    back_to_back = len(last4) == 4 and all(
        80 <= (_date(last4[i]) - _date(last4[i + 1])).days <= 100 for i in range(3))
    if back_to_back:
        for key, t in (("revenue", REV), ("net_income", NI), ("eps_diluted", EPS)):
            vals = [s[t].get(e) for e in last4]
            if all(v is not None for v in vals):
                ttm[key] = round(sum(vals), 2) if key == "eps_diluted" else sum(vals)
    return {"rows": rows, "ttm": ttm}


def changes(result):
    """Plain sentences: what changed in the latest quarter."""
    rows = result["rows"]
    if not rows:
        return []
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    out = []
    line = "%s (quarter ending %s): revenue %s" % (cur["fiscal"], cur["end"], _m(cur["revenue"]))
    if cur["revenue_yoy"] is not None:
        line += " (%+.1f%% vs a year earlier)" % cur["revenue_yoy"]
    line += ", net income %s" % _m(cur["net_income"])
    if cur["net_income_yoy"] is not None:
        line += " (%+.1f%%)" % cur["net_income_yoy"]
    if cur["eps_diluted"] is not None:
        line += ", diluted EPS %s" % inr(cur["eps_diluted"])
        if cur["eps_yoy"] is not None:
            line += " (%+.1f%%)" % cur["eps_yoy"]
    out.append(line + ".")
    for key, name in (("gross_margin", "Gross margin"), ("operating_margin", "Operating margin"),
                      ("net_margin", "Net margin")):
        if cur[key] is None:
            continue
        s = "%s %.1f%%" % (name, cur[key])
        if prev and prev[key] is not None:
            s += " (%+.1f points vs the quarter before)" % (cur[key] - prev[key])
        out.append(s + ".")
    if cur["net_income"] is not None and cur["operating_income"] is not None \
            and cur["net_income"] > cur["operating_income"]:
        out.append("Net income is above operating income: it includes gains outside the business "
                   "(investment income, tax items). Check the filing before reading it as growth.")
    if prev and cur["revenue"] is not None and prev["revenue"]:
        out.append("Revenue %+.1f%% vs the quarter before (many businesses are seasonal: "
                   "compare with a year earlier first)." % pct(cur["revenue"], prev["revenue"]))
    return out


# ---------------------------------------------------------------- output

def _m(v):
    return "n/a" if v is None else crore(v)


def _p(v):
    return "n/a" if v is None else "%+.1f%%" % v


def _pm(v):
    return "n/a" if v is None else "%.1f%%" % v


def render(symbol, name, result, filing):
    out = ["%s (%s). Money in rupees crore (cr), EPS in rupees. Source: %s." % (name or symbol, symbol, SOURCE), ""]
    out.append("%-11s %-8s %15s %8s %7s %7s %7s %15s %8s %8s" % (
        "quarter end", "fiscal", "revenue", "rev yoy", "gross", "oper", "net",
        "net income", "EPS dil", "EPS yoy"))
    for r in result["rows"]:
        out.append("%-11s %-8s %15s %8s %7s %7s %7s %15s %8s %8s" % (
            r["end"], r["fiscal"], _m(r["revenue"]), _p(r["revenue_yoy"]),
            _pm(r["gross_margin"]), _pm(r["operating_margin"]), _pm(r["net_margin"]),
            _m(r["net_income"]), "n/a" if r["eps_diluted"] is None else "%.2f" % r["eps_diluted"],
            _p(r["eps_yoy"])))
    t = result["ttm"]
    out.append("")
    out.append("Last 4 quarters: revenue %s, net income %s, diluted EPS %s." % (
        _m(t.get("revenue")), _m(t.get("net_income")),
        "n/a" if t.get("eps_diluted") is None else "\u20b9%.2f" % t["eps_diluted"]))
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    out.append("")
    if filing:
        out.append("Latest results filing: %s %s, period ended %s, %s, published %s IST" % (
            filing["relating_to"], filing["consolidated"], filing["period_ended"],
            filing["title"], filing["published"]))
        out.append("  " + filing["link"])
    else:
        out.append("Latest results filing: not found in NSE's Financial_Results feed (it lists recent "
                   "filings only). Look on nseindia.com, Corporate Filings, Financial Results.")
    out.append("")
    out.append("Yahoo's figures are unofficial and can lag or differ (consolidated vs standalone, "
               "banks have no gross profit). Check the company's NSE filing before acting.")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- command line

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("symbol", help="NSE symbol, e.g. RELIANCE, M&M")
    ap.add_argument("--quarters", type=int, default=6)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--universe", help="universe file (symbol -> company name), default nse/universe.json")
    ap.add_argument("--offline", metavar="DIR", help="read timeseries-<SYMBOL>.json, Financial_Results.xml, universe.json")
    args = ap.parse_args(argv)
    sym = args.symbol.strip().upper()
    if sym.endswith(".NS"):
        sym = sym[:-3]
    fetcher = Fetcher(offline=args.offline)
    try:
        result = analyse(fetch_series(sym, fetcher=fetcher), quarters=max(1, args.quarters))
    except ResultsError as exc:
        msg = str(exc)
        if msg == "no quarterly results" or msg.startswith("HTTP 404") or "no file" in msg:
            msg = "no quarterly results for %s (check the symbol; Yahoo has none for it)" % sym
        print("results: %s" % msg, file=sys.stderr)
        return 1
    name = load_name(sym, args.universe, args.offline)
    filing, note = None, None
    if name:
        try:
            filing = latest_filing(fetcher.get(RESULTS_RSS, "Financial_Results.xml"), name)
        except ResultsError as exc:
            note = "NSE results feed: %s" % exc
    else:
        note = "no company name for %s in the universe file, so the NSE filing link is skipped" % sym
    if args.json:
        print(json.dumps({"symbol": sym, "name": name, "source": SOURCE, "result": result,
                          "changes": changes(result), "filing": filing, "note": note}, indent=2))
    else:
        sys.stdout.write(render(sym, name, result, filing))
        if note:
            print("NOTE: " + note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
