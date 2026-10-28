#!/usr/bin/env python3
"""Bulk, block and insider deals tracker for nse-deals. Standard library only.

NSE publishes who trades big in a stock and who inside a company trades its shares:

    python3 deals.py today                          # new bulk + block deals, each shown once
    python3 deals.py today --watch STLTECH,TCS      # only those symbols
    python3 deals.py today --min-value-cr 5         # only deals worth 5 crore or more
    python3 deals.py insider                        # new SEBI PIT insider disclosures
    python3 deals.py client "serum institute"       # every deal by a client in your local history

Where the data comes from (public NSE files, no login):
  - https://archives.nseindia.com/content/equities/bulk.csv   bulk deals (> 0.5% of equity), CURRENT day only
  - https://archives.nseindia.com/content/equities/block.csv  block deals (window trades), CURRENT day only
  - https://nsearchives.nseindia.com/content/RSS/Insider_Trading.xml  the latest PIT filings, each linking
    an XBRL file that holds the per-person transactions.
The bulk and block files are overwritten every day, so this tool keeps its own history in
nse/state/deals.json (one row per fingerprint, with first_seen). `client` searches that history.
The insider RSS feed can lag by months; when its newest item is over 7 days old the tool says so.

These are public filings, not tips. Bulk and block deals are reported after the trade; PIT filings
arrive up to two trading days after it. Not financial advice.

State: `today` and `insider` remember what they showed, so each row alerts once. Watch symbols come from
--watch, plus nse/watchlist.json and nse/portfolio.json (rows in those get a star marker).
Offline: --offline DIR reads bulk.csv, block.csv, Insider_Trading.xml and the IT_*.xml files in DIR.
"""

import argparse
import csv
import datetime as _dt
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
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

BULK_URL = "https://archives.nseindia.com/content/equities/bulk.csv"
BLOCK_URL = "https://archives.nseindia.com/content/equities/block.csv"
RSS_URL = "https://nsearchives.nseindia.com/content/RSS/Insider_Trading.xml"
UA = "Mozilla/5.0 (nse-day-trader-skill)"
NSE_PAUSE = 0.25
STALE_DAYS = 7
FIRST_DAYS = 7
STALE_HELP = "check nseindia.com > Corporate Filings > Insider Trading"
MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
STAR = "★ watching"


class DealsError(Exception):
    pass


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
                raise DealsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read().decode("utf-8-sig", "replace")
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
        except urllib.error.HTTPError as exc:
            raise DealsError("%s: HTTP %s" % (url, exc.code))
        except (urllib.error.URLError, OSError) as exc:
            raise DealsError("%s: %s" % (url, getattr(exc, "reason", exc)))
        finally:
            self._last = time.time()
        return data.decode("utf-8-sig", "replace")


# ---------------------------------------------------------------- parsing

def parse_date(text):
    return _dt.date.fromisoformat(text)


def _iso_nse_date(text):
    """28-OCT-2026 -> 2026-10-28 ('' if it is not a date)."""
    m = re.match(r"^\s*(\d{1,2})[-/ ]([A-Za-z]{3})[A-Za-z]*[-/ ](\d{4})\s*$", text or "")
    if not m or m.group(2).upper() not in MONTHS:
        return ""
    return "%s-%02d-%02d" % (m.group(3), MONTHS[m.group(2).upper()], int(m.group(1)))


def _num(text):
    try:
        return Decimal(str(text).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def parse_deals(csv_text, kind):
    """NSE bulk.csv / block.csv text -> rows: date (ISO), symbol, name, client, side, qty, price, value_inr, kind.
    value_inr is qty x price exactly. Rows that are blank or malformed are skipped."""
    reader = csv.reader(io.StringIO(csv_text.lstrip("﻿")))
    head = None
    out = []
    for rec in reader:
        cells = [c.strip() for c in rec]
        if head is None:
            if cells and cells[0].lower() == "date":
                head = [c.lower() for c in cells]
            continue
        if len(cells) < len(head) - 1 or not any(cells):
            continue

        def col(*names):
            for i, h in enumerate(head):
                if any(h.startswith(n) for n in names) and i < len(cells):
                    return cells[i]
            return ""
        day = _iso_nse_date(col("date"))
        sym = col("symbol").upper()
        side = col("buy").upper()
        qty, price = _num(col("quantity")), _num(col("trade price"))
        if not (day and sym and side[:1] in ("B", "S") and qty is not None and price is not None):
            continue
        q = int(qty)
        out.append({"date": day, "symbol": sym, "name": col("security"), "client": col("client"),
                    "side": "BUY" if side[0] == "B" else "SELL", "qty": q, "price": price,
                    "value_inr": Decimal(q) * price, "kind": kind})
    return out


def _local(tag):
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _category(text):
    low = (text or "").lower()
    if "promoter" in low:
        return "Promoter"
    if "director" in low:
        return "Director"
    if "key managerial" in low or "kmp" in low:
        return "KMP"
    if "designated" in low:
        return "Designated person"
    if "relative" in low:
        return "Immediate relative"
    return (text or "").strip() or "Other"


def _side(*texts):
    for t in texts:
        low = (t or "").lower()
        if not low.strip():
            continue
        if any(w in low for w in ("pledge", "invoc", "revoc", "revoke")):
            return "PLEDGE" if "pledge" in low else low.strip().upper()
        if any(w in low for w in ("sell", "sale", "dispos")):
            return "SELL"
        if any(w in low for w in ("buy", "purchase", "acqui", "allot")):
            return "BUY"
    return ""


def parse_pit_xbrl(xml):
    """A SEBI PIT disclosure (NSE XBRL, namespaces vary so elements are matched by local name) -> one row per
    transaction: symbol, company, person, category, side, qty, value_inr, from_date, to_date, mode."""
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except ET.ParseError as exc:
        raise DealsError("could not read the XBRL file: %s" % exc)
    groups = {}
    order = []
    for el in root.iter():
        if len(el) or el.text is None or not el.text.strip():
            continue
        ctx = el.get("contextRef") or ""
        if ctx not in groups:
            groups[ctx] = {}
            order.append(ctx)
        groups[ctx].setdefault(_local(el.tag), el.text.strip())
    head = {}
    for ctx in order:
        if "NameOfThePerson" not in groups[ctx]:
            head.update(groups[ctx])
    rows = []
    for ctx in order:
        g = groups[ctx]
        if "NameOfThePerson" not in g:
            continue
        qty = _num(g.get("SecuritiesAcquiredOrDisposedNumberOfSecurity"))
        val = _num(g.get("SecuritiesAcquiredOrDisposedValueOfSecurity", g.get("ValueInAggregate")))
        mode = g.get("ModeOfAcquisitionOrDisposal", "")
        rows.append({
            "symbol": (head.get("NSESymbol") or "").upper(),
            "company": head.get("NameOfTheCompany", ""),
            "person": g["NameOfThePerson"],
            "category": _category(g.get("CategoryOfPerson")),
            "side": _side(g.get("SecuritiesAcquiredOrDisposedTransactionType"), mode),
            "qty": int(qty) if qty is not None else 0,
            "value_inr": val if val is not None else Decimal(0),
            "from_date": g.get("DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate", ""),
            "to_date": g.get("DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate", ""),
            "mode": mode,
        })
    return rows


def parse_insider_rss(xml):
    """Insider_Trading.xml -> [{company, link, filed 'YYYY-MM-DDTHH:MM+05:30', type}], newest first as published."""
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except ET.ParseError as exc:
        raise DealsError("could not read the insider RSS feed: %s" % exc)
    items = []
    for it in root.iter("item"):
        g = lambda t: (it.findtext(t) or "").strip()
        filed = ""
        m = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{1,2}):(\d{2})", g("pubDate"))
        if m and m.group(2).upper() in MONTHS:
            filed = "%s-%02d-%02dT%02d:%s+05:30" % (m.group(3), MONTHS[m.group(2).upper()], int(m.group(1)),
                                                    int(m.group(4)), m.group(5))
        items.append({"company": g("title"), "link": g("link"), "filed": filed,
                      "type": re.sub(r"^TYPE OF SECURITY\s*:\s*", "", g("description"))})
    return items


# ---------------------------------------------------------------- state

def load_state(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
        st.setdefault("seen", [])
        st.setdefault("history", {})
        st.setdefault("last_check", None)
        return st
    return {"seen": [], "history": {}, "last_check": None}


def save_state(path, state):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _plain(row):
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in row.items()}


def fingerprint(row):
    """A stable id over every field of the row, so a row is shown once and a changed row is new."""
    blob = json.dumps(_plain(row), sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def new_since(rows, state, today, first_days=FIRST_DAYS):
    """Rows not shown before. On the very first run only rows dated within first_days (None: all of them)."""
    seen = set(state.get("seen", []))
    fresh, batch = [], set()
    for r in rows:
        fp = fingerprint(r)
        if fp not in seen and fp not in batch:
            batch.add(fp)
            fresh.append(r)
    if state.get("last_check") is None and first_days is not None:
        cutoff = (today - _dt.timedelta(days=first_days)).isoformat()
        fresh = [r for r in fresh if (r.get("date") or r.get("to_date") or "9999") >= cutoff]
    return fresh


def remember(state, rows, shown, when):
    """History keeps every fetched row (client search); `seen` only what was shown, so filtered rows come back."""
    for r in rows:
        fp = fingerprint(r)
        if fp not in state["history"]:
            d = _plain(r)
            d["first_seen"] = when
            state["history"][fp] = d
    state["seen"] = sorted(set(state["seen"]) | {fingerprint(r) for r in shown})
    state["last_check"] = when


# ---------------------------------------------------------------- symbols to watch

def _load_json(path):
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except ValueError:
            return None
    return None


def file_symbols(watchlist, portfolio):
    out = set()
    for h in (_load_json(watchlist) or {}).get("symbols", []):
        s = h.get("symbol") if isinstance(h, dict) else h
        if s:
            out.add(str(s).upper())
    for p in (_load_json(portfolio) or {}).get("positions", []):
        s = p.get("symbol") or p.get("ticker")
        if s:
            out.add(str(s).upper())
    return out


def watch_sets(args):
    """(limit or None, marked): --watch SYM,... limits; a bare --watch limits to the files' symbols."""
    files = file_symbols(args.watchlist, args.portfolio)
    if args.watch is None:
        return None, files
    named = {s.strip().upper() for s in args.watch.split(",") if s.strip()}
    return (named or files), files | named


# ---------------------------------------------------------------- output

def money(x):
    x = Decimal(x)
    return crore(x) if x >= Decimal(1000000) else inr(x, 0)


def describe_deal(r, marked=()):
    line = "%s  %-5s %-12s %s %s @ %s = %s  [%s]  %s" % (
        r["date"], r["side"], r["symbol"], r["client"], "{:,}".format(r["qty"]), inr(r["price"]),
        money(r["value_inr"]), r["kind"], r["name"])
    return line + ("  " + STAR if r["symbol"] in marked else "")


def describe_insider(r, marked=()):
    line = "%s  %-6s %-12s %s (%s) %s shares = %s, %s to %s, %s" % (
        r.get("filed", "")[:10] or r.get("to_date", ""), r["side"] or "?", r["symbol"], r["person"], r["category"],
        "{:,}".format(r["qty"]), money(r["value_inr"]), r["from_date"] or "?", r["to_date"] or "?", r["mode"] or "?")
    return line + ("  " + STAR if r["symbol"] in marked else "")


def describe_history(r):
    if r.get("kind") == "insider":
        return describe_insider(r) + "  [insider, first seen %s]" % r["first_seen"][:10]
    d = dict(r, qty=int(r["qty"]), price=Decimal(r["price"]), value_inr=Decimal(r["value_inr"]))
    return describe_deal(d) + "  [first seen %s]" % r["first_seen"][:10]


# ---------------------------------------------------------------- CLI

def _parser():
    ap = argparse.ArgumentParser(description="NSE bulk, block and insider deals tracker.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p, watch=True):
        p.add_argument("--state", default="nse/state/deals.json")
        p.add_argument("--watchlist", default="nse/watchlist.json")
        p.add_argument("--portfolio", default="nse/portfolio.json")
        p.add_argument("--offline", metavar="DIR", help="read bulk.csv, block.csv, Insider_Trading.xml, IT_*.xml here")
        p.add_argument("--now", help='IST clock for tests, e.g. "2026-10-29 10:15"')
        p.add_argument("--dry-run", action="store_true", help="do not remember what was shown")
        p.add_argument("--json", action="store_true")
        if watch:
            p.add_argument("--watch", nargs="?", const="", default=None, metavar="SYM,...",
                           help="only these symbols (bare --watch: your watchlist and portfolio)")
    t = sub.add_parser("today", help="new bulk and block deals")
    common(t)
    t.add_argument("--min-value-cr", type=Decimal, default=Decimal(0), help="hide deals below this many crore")
    i = sub.add_parser("insider", help="new SEBI PIT insider disclosures")
    common(i)
    i.add_argument("--limit", type=int, default=20, help="newest RSS items to open")
    c = sub.add_parser("client", help="every deal by a client or insider in your local history")
    c.add_argument("name")
    c.add_argument("--state", default="nse/state/deals.json")
    c.add_argument("--json", action="store_true")
    return ap


def main(argv=None):
    ap = _parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    try:
        return _run(args)
    except (DealsError, ValueError, OSError) as exc:
        print("deals: %s" % exc, file=sys.stderr)
        return 2


def _finish(args, state, rows, shown, now):
    if not args.dry_run:
        remember(state, rows, shown, now.isoformat(timespec="minutes"))
        save_state(args.state, state)


def _run(args):
    if args.cmd == "client":
        needle = args.name.strip().lower()
        state = load_state(args.state)
        hits = [r for r in state["history"].values()
                if needle in (r.get("client") or r.get("person") or "").lower()]
        hits.sort(key=lambda r: (r.get("date") or r.get("to_date") or "", r["first_seen"]), reverse=True)
        if args.json:
            print(json.dumps(hits, indent=2, ensure_ascii=False))
        elif not hits:
            print('No deals by "%s" in your history (%s). History starts the first time you run `today` or `insider`.'
                  % (args.name, args.state))
        else:
            for r in hits:
                print(describe_history(r))
        return 0 if hits else 1

    now = to_ist(parse_now(args.now))
    today = now.date()
    fetcher = Fetcher(offline=args.offline)
    state = load_state(args.state)
    limit, marked = watch_sets(args)

    if args.cmd == "today":
        ok, why = trading_day(today)
        if not ok:
            print("no deals: market closed (%s). NSE publishes bulk and block deals for trading days only." % why)
            return 0
        rows = []
        for url, name, kind in ((BULK_URL, "bulk.csv", "bulk"), (BLOCK_URL, "block.csv", "block")):
            try:
                rows += parse_deals(fetcher.get(url, name), kind)
            except DealsError as exc:
                print("deals: %s: skipped (%s)" % (kind, exc), file=sys.stderr)
        fresh = new_since(rows, state, today)
        shown = [r for r in fresh
                 if (limit is None or r["symbol"] in limit)
                 and r["value_inr"] >= args.min_value_cr * Decimal(10000000)]
        shown.sort(key=lambda r: (r["date"], r["value_inr"]), reverse=True)
        if args.json:
            print(json.dumps({"new": [_plain(r) for r in shown]}, indent=2, ensure_ascii=False))
        elif not shown:
            print("Today: nothing new, no bulk or block deals you have not seen (%d fetched)." % len(rows))
        else:
            print("New deals (%d of %d fetched; public filings, not tips):" % (len(shown), len(rows)))
            for r in shown:
                print(describe_deal(r, marked))
        _finish(args, state, rows, shown, now)
        return 0

    # insider
    items = parse_insider_rss(fetcher.get(RSS_URL, "Insider_Trading.xml"))
    stamps = [it["filed"] for it in items if it["filed"]]
    if stamps:
        latest = max(stamps)[:10]
        if (today - parse_date(latest)).days > STALE_DAYS:
            print("NSE's insider-trading feed looks stale (latest %s); %s" % (latest, STALE_HELP),
                  file=sys.stderr if args.json else sys.stdout)
    rows = []
    uniq, links = [], set()
    for it in items:
        if it["link"] not in links:
            links.add(it["link"])
            uniq.append(it)
    for it in uniq[:max(0, args.limit)]:
        name = it["link"].rsplit("/", 1)[-1]
        try:
            for r in parse_pit_xbrl(fetcher.get(it["link"], name)):
                r.update({"kind": "insider", "filed": it["filed"], "source": name})
                rows.append(r)
        except DealsError as exc:
            print("deals: %s: skipped (%s)" % (name, exc), file=sys.stderr)
    fresh = new_since(rows, state, today, first_days=None)
    shown = [r for r in fresh if limit is None or r["symbol"] in limit]
    shown.sort(key=lambda r: (r["filed"], r["value_inr"]), reverse=True)
    if args.json:
        print(json.dumps({"new": [_plain(r) for r in shown]}, indent=2, ensure_ascii=False))
    elif not shown:
        print("Insider: nothing new, no insider disclosures you have not seen (%d read)." % len(rows))
    else:
        print("New insider disclosures (%d; SEBI PIT filings, public, not tips):" % len(shown))
        for r in shown:
            print(describe_insider(r, marked))
    _finish(args, state, rows, shown, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
