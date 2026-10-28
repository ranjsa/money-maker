#!/usr/bin/env python3
"""Price alerts and the NSE clock for nse-alerts. Standard library only.

    python3 alerts.py clock                          # is the NSE open? next open
    python3 alerts.py calendar                       # the NSE holiday table this tool knows
    python3 alerts.py calendar --refresh             # diff it against NSE's holiday-master feed (never edits code)
    python3 alerts.py quote RELIANCE --write         # quotes -> nse/prices.json (nse-risk needs the previous close)
    python3 alerts.py quote --holdings --write       # every symbol in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by time from nse/plan.json
    python3 alerts.py add --symbol RELIANCE --stop 1150
    python3 alerts.py add --symbol RELIANCE --target 1200
    python3 alerts.py add --at 15:10 --label "flat by"
    python3 alerts.py add --symbol TCS --pct 3
    python3 alerts.py add --opening-bell
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-10-28 15:10" --offline tests/fixtures/nse-alerts
    python3 alerts.py list
    python3 alerts.py reset reliance-plan-stop

Rule types (rules live in nse/alerts.json, times are IST):
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (Yahoo's chart meta; or from "ref" if you give one); "direction": "up" or "down"
  opening_bell    the NSE cash market opened (09:15 IST on a trading day; a special
                  session rings at its start); never on weekends or holidays
  time            the IST clock passed "at" (HH:MM) on a trading day
  orphan_leg      automatic, no rule to add: when nse/state/broker.json shows a stop,
                  target or exit leg filled today while the sibling exit leg was placed
                  and neither cancelled nor filled, `check` rings once to cancel it

Every rule rings at most once per day (state in nse/state/alerts.json). An above /
below alert rings ONCE and then stays quiet until you reset it, unless the rule has
"rearm": true (it re-arms when the price crosses back). A rule with "date" is only
watched on that IST date (rules from a plan are for the plan's day only).

Quotes (free, delayed, unofficial; for alerts only, never for timing an order):
  yahoo   https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS (falls back to
          query1 on a 429 or a 5xx; one retry with a pause on a 429; one request per
          symbol, half a second apart). Stooq has no reliable NSE coverage and is gone.
  --offline DIR reads DIR/yahoo-<SYMBOL>.json (or .json.gz) instead.

NSE calendar: the 2026 CM holidays from https://www.nseindia.com/api/holiday-master?type=trading
(key CM). Any other year is treated as Mon-Fri open with a warning; `calendar --refresh`
fetches the feed and prints what changed so you can update NSE_HOLIDAYS.
"""

import argparse
import datetime
import gzip
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
PAUSE = 0.5    # seconds between requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
REFRESH_HINT = "run nse-alerts/alerts.py calendar --refresh or check nseindia.com"
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                          "nov", "dec"])}
EXIT_LEGS = ("stop", "target", "exit")


class AlertError(Exception):
    pass


def calendar_warning(d):
    """'' for a year the holiday table covers, else the standard out-of-calendar warning."""
    if d.year in CALENDAR_YEARS:
        return ""
    return "NSE calendar for %d not loaded: %s" % (d.year, REFRESH_HINT)


def _norm(sym):
    s = str(sym).strip().upper()
    return s[:-3] if s.endswith(".NS") else s


def _show(stamp):
    """'2026-10-29T09:15+05:30' -> '2026-10-29 09:15 IST'."""
    return str(stamp).replace("T", " ").replace("+05:30", " IST")


# ---------------------------------------------------------------- quotes

def yahoo_url(sym, host=YAHOO_HOSTS[0]):
    return "https://%s/v8/finance/chart/%s?range=1d&interval=1d" % (host, yahoo_quote(_norm(sym)))


def parse_yahoo(text, sym):
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise AlertError("Yahoo has no data for %s: %s" % (sym, err.get("description", "unknown symbol")))
    m = res[0].get("meta", {})
    price = m.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % sym)
    t = m.get("regularMarketTime")
    when = (datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if t else "")
    q = (res[0].get("indicators", {}).get("quote") or [{}])[0]
    opens = [x for x in (q.get("open") or []) if x is not None]
    return {"symbol": _norm(sym), "price": float(price), "open": opens[0] if opens else None,
            "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"),
            "prev_close": m.get("chartPreviousClose") or m.get("previousClose"), "time": when,
            "currency": m.get("currency"), "source": "yahoo"}


_urlopen = urllib.request.urlopen   # tests swap these two
_sleep = time.sleep


def _get(url, timeout=20):
    """(HTTP status, body). A network failure is an AlertError."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with _urlopen(req, timeout=timeout) as r:
            return 200, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body
    except urllib.error.URLError as e:
        raise AlertError("could not reach %s: %s" % (url.split("/")[2], e.reason))


def fetch(url, timeout=20):
    code, text = _get(url, timeout)
    if code != 200:
        raise AlertError("%s answered HTTP %d" % (url.split("?")[0], code))
    return text


def fetch_yahoo(sym, timeout=20):
    """query2 first. A 429 waits BACKOFF seconds and tries once more; a 429 or a 5xx
    after that moves on to query1. A 404 comes back as Yahoo's own error JSON."""
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(sym, host)
        code, text = _get(url, timeout)
        if code == 429:
            _sleep(BACKOFF)
            code, text = _get(url, timeout)
        if code == 200:
            return text
        if code == 404 and text.strip().startswith("{"):
            return text
        last = (url, code)
        if code != 429 and code < 500:
            break
    raise AlertError("%s answered HTTP %d" % (last[0].split("?")[0], last[1]))


def _offline_text(folder, name):
    base = os.path.join(folder, name)
    if os.path.exists(base):
        with open(base, encoding="utf-8") as fh:
            return fh.read()
    if os.path.exists(base + ".gz"):
        with gzip.open(base + ".gz", "rt", encoding="utf-8") as fh:
            return fh.read()
    raise AlertError("no offline file %s" % base)


def get_quotes(symbols, offline=None, pause=PAUSE):
    """{SYM: quote} and [errors]. One request per symbol, with a pause between."""
    quotes, errors = {}, []
    for i, sym in enumerate(symbols):
        sym = _norm(sym)
        try:
            if offline:
                text = _offline_text(offline, "yahoo-%s.json" % sym)
            else:
                if i and pause:
                    _sleep(pause)
                text = fetch_yahoo(sym)
            quotes[sym] = parse_yahoo(text, sym)
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


def write_prices(path, quotes, now_utc, source="yahoo"):
    """Merge quotes into prices.json, the shape nse-risk and nse-budget read."""
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (ValueError, OSError):
            old = {}
    if not isinstance(old, dict):
        old = {}
    prices = dict(old.get("prices") or {})
    detail = dict(old.get("detail") or {})
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"prev_close": q.get("prev_close"), "time": q.get("time"), "source": q.get("source"),
                     "fetched": stamp}
    doc = {"as_of": stamp, "source": source, "currency": "INR",
           "note": ("Delayed quotes for alerts and sizing checks. Your broker's price is the one that fills. "
                    "detail.time is the quote's own time, detail.fetched is when it was read, "
                    "detail.prev_close is Yahoo's previous close."),
           "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- rules

PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "opening_bell", "time"}
NO_QUOTE = {"opening_bell", "time"}


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def validate(rule):
    t = rule.get("type")
    if t not in PRICE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as an IST time like \"15:10\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    """A rule with a "date" is watched on that IST date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    """Did a rule that rang at this UTC stamp ring on this IST date?"""
    if not stamp:
        return False
    try:
        utc = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return stamp[:10] == today
    return to_ist(utc).date().isoformat() == today


def orphan_legs(broker, today):
    """Exit legs left open after a sibling filled today, from nse/state/broker.json.

    A stop, target or exit (flatten) leg filled today, while a sibling stop or target leg of the same
    group was placed and has no cancelled or filled record: that leg can still trigger. Returns
    [{id, symbol, leg, sibling, message}]."""
    recs = broker.get("orders") if isinstance(broker, dict) else None
    groups = {}
    for r in recs if isinstance(recs, list) else []:
        if isinstance(r, dict):
            groups.setdefault((r.get("group"), r.get("fingerprint")), []).append(r)
    out, seen = [], set()
    for (group, _fp), rs in groups.items():
        for f in rs:
            leg = f.get("leg")
            if f.get("status") != "filled" or leg not in EXIT_LEGS or str(f.get("date")) != today:
                continue
            for sib in ("stop", "target"):
                if sib == leg:
                    continue
                sib_recs = [r for r in rs if r.get("leg") == sib]
                placed = any(r.get("status") == "placed" for r in sib_recs)
                closed = any(r.get("status") in ("cancelled", "canceled", "filled") for r in sib_recs)
                oid = "orphan-%s-%s" % (group, sib)
                if placed and not closed and oid not in seen:
                    seen.add(oid)
                    sym = str(f.get("symbol") or "").upper()
                    out.append({"id": oid, "symbol": sym, "leg": leg, "sibling": sib,
                                "message": "%s: %s filled, cancel the %s leg now (nse-broker/ticket.py fill printed "
                                           "the cancel_order ticket)" % (sym or str(group), leg, sib)})
    return out


def evaluate(rules, quotes, status, state, now_utc, cfg=None, broker=None):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}.

    `status` is market_status(now_utc, cfg); `broker` is the parsed nse/state/broker.json (orphan legs)."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    local = to_ist(now_utc)
    today = local.date().isoformat()
    sess = session(local.date(), cfg)
    is_open = status.get("state") == "open"
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if is_open and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message": "Opening bell: the NSE cash market is open "
                              "(%s IST)." % local.strftime("%H:%M")})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = _hhmm(r.get("at"))
            if sess and local.time() >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s IST (set for %s)" % (label[:1].upper(), label[1:],
                                                           local.strftime("%H:%M"), at.strftime("%H:%M"))
                msg += (", the market closes at %s." % sess[1].strftime("%H:%M")) if is_open else \
                    ", the market has closed."
                sym = str(r.get("symbol") or "").upper()
                if sym:
                    msg += (" If %s is still open, close it: nse-broker/ticket.py flatten --symbol %s builds the "
                            "sell ticket, then nse-risk and your yes." % (sym, sym))
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = _norm(r["symbol"])
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s %s %s: %s your %s %s%s."
                              % (sym, "is" if is_open else "last", inr(p), word, label, inr(r["price"]),
                                 (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to %s (from %s)."
                              % (sym, move, inr(p), inr(ref))})
                rung[rid] = stamp
    for o in orphan_legs(broker, today):
        if not _rang_today(rung.get(o["id"]), today):
            rings.append({"id": o["id"], "symbol": o["symbol"], "message": o["message"]})
            rung[o["id"]] = stamp
    state["last_check"] = stamp
    return rings, state


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def rules_from_plan(plan, cfg_flat_by=None):
    """nse/plan.json -> a stop alert, a target alert and a flat-by time alert, for the plan's day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run nse-exit first)")
    sym = _norm(plan["symbol"])
    out = []
    for key, typ, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run nse-exit)" % key)
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": typ, "price": float(v),
                    "note": word + (": " + label if label else "")})
    flat = plan.get("flat_by") or cfg_flat_by or "15:10"
    if _hhmm(flat) is None:
        raise AlertError("the plan's flat_by %r is not a time like 15:10" % flat)
    out.append({"id": "%s-flat-by" % sym.lower(), "symbol": sym, "type": "time", "at": flat, "label": "flat by"})
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- calendar

def parse_feed_date(text):
    """'26-Jan-2026' -> datetime.date, or None."""
    m = re.match(r"^\s*(\d{1,2})-([A-Za-z]{3})[A-Za-z]*-(\d{4})\s*$", str(text or ""))
    if not m or m.group(2).lower() not in MONTHS:
        return None
    try:
        return datetime.date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError:
        return None


def feed_holidays(doc, year):
    """holiday-master JSON -> {'YYYY-MM-DD': name} for the CM segment, `year`, weekdays only."""
    cm = doc.get("CM") if isinstance(doc, dict) else None
    if not isinstance(cm, list):
        raise AlertError("the holiday feed has no CM list (NSE may have changed the format): check nseindia.com")
    out = {}
    for h in cm:
        d = parse_feed_date(h.get("tradingDate")) if isinstance(h, dict) else None
        if d and d.year == year and d.weekday() < 5:
            out[d.isoformat()] = str(h.get("description") or "").strip()
    return out


def diff_holidays(feed, year):
    """[(kind, date, text)] between the feed and NSE_HOLIDAYS for `year`: added, removed, renamed."""
    mine = {d: n for d, n in NSE_HOLIDAYS.items() if d.startswith("%d-" % year)}
    rows = []
    for d in sorted(set(feed) | set(mine)):
        if d in feed and d not in mine:
            rows.append(("added", d, feed[d]))
        elif d in mine and d not in feed:
            rows.append(("removed", d, mine[d]))
        elif feed[d] != mine[d]:
            rows.append(("renamed", d, "%s -> %s" % (mine[d], feed[d])))
    return rows


def cmd_calendar(a):
    if not a.refresh:
        years = ", ".join(str(y) for y in CALENDAR_YEARS)
        print("NSE CM trading holidays this tool knows (%s), weekdays only: %d holidays." % (years, len(NSE_HOLIDAYS)))
        for d in sorted(NSE_HOLIDAYS):
            print("%s  %-9s  %s" % (d, datetime.date.fromisoformat(d).strftime("%A"), NSE_HOLIDAYS[d]))
        print("Other years are treated as Mon-Fri open. To compare with NSE's own list: "
              "alerts.py calendar --refresh")
        return 0
    year = a.year or to_ist(parse_now(None)).year
    text = _offline_text(a.offline, "holiday-master.json") if a.offline else None
    if text is None:
        try:
            text = fetch(HOLIDAY_URL)
        except AlertError as e:
            raise AlertError("%s (NSE sometimes turns scripts away: open nseindia.com > Resources > Holidays in a "
                             "browser and compare by hand)" % e)
    try:
        doc = json.loads(text)
    except ValueError:
        raise AlertError("the holiday feed was not JSON (NSE may be blocking scripts): check nseindia.com")
    feed = feed_holidays(doc, year)
    print("NSE CM holidays for %d (weekdays only): %d" % (year, len(feed)))
    for d in sorted(feed):
        print("%s  %-9s  %s" % (d, datetime.date.fromisoformat(d).strftime("%A"), feed[d]))
    rows = diff_holidays(feed, year)
    print("")
    for kind, d, t in rows:
        print("%-7s %s  %s" % (kind, d, t))
    print("%d difference%s vs NSE_HOLIDAYS." % (len(rows), "" if len(rows) == 1 else "s"))
    if year not in CALENDAR_YEARS:
        print("%d is not in CALENDAR_YEARS %s: the tools treat it as Mon-Fri open until the year is added together "
              "with its holidays." % (year, CALENDAR_YEARS))
    if rows:
        print("To adopt them, edit NSE_HOLIDAYS (and CALENDAR_YEARS) in the nse common block of every tool; "
              "tests/sync_common.py keeps the copies identical.")
    print("This command never edits code or config.")
    return 1 if rows else 0


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        syms.append(_norm(p.get("ticker") or p.get("symbol") or ""))
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(_norm(plan["symbol"]))
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def _cfg(a):
    cfg = _read(getattr(a, "config", None), {})
    return cfg if isinstance(cfg, dict) else {}


def cmd_clock(a):
    now = parse_now(a.now)
    cfg = _cfg(a)
    st = market_status(now, cfg)
    local = to_ist(now)
    st = dict(st, now_ist=local.strftime("%Y-%m-%d %H:%M"), date=local.date().isoformat(),
              weekday=local.strftime("%A"), calendar_known=local.year in CALENDAR_YEARS)
    s = session(local.date(), cfg)
    if s:
        st["opens"], st["closes"] = s[0].strftime("%H:%M"), s[1].strftime("%H:%M")
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        print("IST %s (%s): NSE %s%s." % (st["now_ist"], st["weekday"], "OPEN" if st["state"] == "open" else st["state"],
                                          (", " + st["reason"]) if st["reason"] else ""))
        if s:
            print("Today's session: %s to %s IST." % (st["opens"], st["closes"]))
        if st["state"] != "open" and st["next_open"]:
            print("Next open: %s." % _show(st["next_open"]))
        warn = calendar_warning(local.date())
        if warn:
            print(warn)
    return 0 if st["state"] == "open" else 1


def cmd_quote(a):
    syms = _dedupe([_norm(s) for s in a.symbols] + (holdings(a) if a.holdings else []))
    if not syms:
        raise AlertError("which symbols? alerts.py quote RELIANCE TCS, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(syms, a.offline)
    if a.write and quotes:
        write_prices(a.write, quotes, now)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
    else:
        for s in syms:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("prev_close"):
                    chg = " (%+.2f%% vs prev close %s)" % (100.0 * (q["price"] / q["prev_close"] - 1),
                                                           inr(q["prev_close"]))
                when = ""
                if q.get("time"):
                    when = "  " + to_ist(datetime.datetime.strptime(q["time"], "%Y-%m-%dT%H:%M:%SZ")).strftime(
                        "%Y-%m-%d %H:%M") + " IST"
                print("%-10s %s%s%s  [%s]" % (s, inr(q["price"]), chg, when, q["source"]))
                if not q.get("prev_close"):
                    print("warning: Yahoo gave no previous close for %s: nse-risk blocks an entry without one "
                          "(try again, or check the broker)" % s)
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        print("Quotes are free, delayed and unofficial: for alerts only. Your broker's price is the one that fills.")
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    rules = doc.get("rules") if isinstance(doc, dict) else None
    rules = rules or []
    broker = _read(a.broker, None)
    if not rules and not (isinstance(broker, dict) and broker.get("orders")):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    cfg = _cfg(a)
    now = parse_now(a.now)
    local = to_ist(now)
    status = market_status(now, cfg)
    today = local.date().isoformat()
    watched = [r for r in rules if active(r, today)]
    syms = _dedupe([_norm(r.get("symbol", "")) for r in watched if r.get("type") not in NO_QUOTE])
    quotes, errors = (get_quotes(syms, a.offline) if syms else ({}, []))
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, status, state, now, cfg, broker)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now)
    if a.json:
        print(json.dumps({"market": status, "now_ist": local.strftime("%Y-%m-%d %H:%M"), "rings": rings,
                          "errors": errors, "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2))
    else:
        stamp = local.strftime("%Y-%m-%d %H:%M")
        if status["state"] == "open":
            print("NSE OPEN (%s IST)." % stamp)
        else:
            print("NSE %s (%s IST%s)." % (status["state"], stamp, (", " + status["reason"]) if status["reason"] else ""))
        warn = calendar_warning(local.date())
        if warn:
            print(warn)
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s %s" % (s, inr(quotes[s]["price"])) for s in syms if s in quotes))
                if quotes else ""))
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order. Check your positions and open orders at the broker "
                  "and record any fill (nse-broker/ticket.py fill). To close a position yourself: ticket.py "
                  "flatten, then nse-risk and your yes.")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run nse-exit first)" % a.plan)
        cfg = _cfg(a)
        new = rules_from_plan(plan, cfg.get("flat_by"))
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(a.state, {})
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r)))
        print("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). "
              "Free quotes are delayed; the stop-loss and target orders at the broker are what actually act.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants an IST time like 15:10")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = _norm(a.symbol)
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell)")
        sym = _norm(a.symbol)
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        val = rule.get("price", rule.get("pct"))
        rule = dict({"id": a.id or ("%s-%s-%s" % (sym.lower(), rule["type"], _n(val)))}, symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Price alerts and the NSE clock (nse-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--offline", metavar="DIR", help="read yahoo-<SYMBOL>.json files, no network")
        p.add_argument("--now", help="pretend it is this time (no offset = IST)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is the NSE open?")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p.add_argument("--config", default="nse/config.json")
    p = sub.add_parser("calendar", help="the NSE holiday table; --refresh diffs it against NSE's feed")
    p.add_argument("--refresh", action="store_true", help="fetch holiday-master?type=trading and print the diff")
    p.add_argument("--offline", metavar="DIR", help="read DIR/holiday-master.json instead of the network")
    p.add_argument("--year", type=int, help="year to compare (default: this year, IST)")
    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add symbols from your portfolio and plan")
    p.add_argument("--portfolio", default="nse/portfolio.json")
    p.add_argument("--plan", default="nse/plan.json")
    p.add_argument("--write", nargs="?", const="nse/prices.json", metavar="PATH",
                   help="save to prices.json (default nse/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="nse/alerts.json")
    p.add_argument("--state", default="nse/state/alerts.json")
    p.add_argument("--prices-out", default="nse/prices.json")
    p.add_argument("--broker", default="nse/state/broker.json", help="orders log, for the orphan-leg alert")
    p.add_argument("--config", default="nse/config.json")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="nse/alerts.json")
    p.add_argument("--state", default="nse/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--opening-bell", action="store_true")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, IST")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by time from the plan")
    p.add_argument("--plan", default="nse/plan.json")
    p.add_argument("--config", default="nse/config.json", help="flat_by when the plan has none")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by\"")
    p.add_argument("--date", help="watch this rule on this IST date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="nse/alerts.json")
    p.add_argument("--state", default="nse/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="nse/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "calendar": cmd_calendar, "quote": cmd_quote, "check": cmd_check,
                "add": cmd_add, "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except (AlertError, ValueError) as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
