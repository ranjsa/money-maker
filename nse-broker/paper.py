#!/usr/bin/env python3
"""Local paper engine for nse-broker. Standard library only; the network only for Yahoo 5-minute bars.

No Indian broker offers a paper (demo) account, so nse-broker plays your
approved order group here on the stock's real 5-minute bars instead of
sending it anywhere:

  run      the group you said yes to (ticket.py approve):
             entry   fills at the OPEN of the first 5-minute bar that starts
                     at or after the approval time (a LIMIT entry fills there
                     only if the open is at or under the limit, else at the
                     limit once a bar's low reaches it)
             stop    a bar whose low reaches the trigger fills at the trigger,
                     or at the bar's open when it opens below it (a gap)
             target  a bar whose high reaches the price fills at the price,
                     or at the bar's open when it opens above it
             both    the stop and the target inside one bar: the stop is
                     assumed first (the conservative call)
             flat_by nothing hit: squared off at the close of the bar that
                     ends at flat_by (15:10 -> the 15:05 to 15:10 bar)
           P&L = (exit - entry) x qty - round-trip charges. The fills go to
           nse/state/broker.json in the same shape as live fills, so nse-risk
           counts paper trades, orders and max loss exactly like live ones
           (the max loss is measured from the entry fill: qty x (fill - stop)
           plus the round-trip charges). If the bars stop before flat_by (run
           during the day), the position stays open; run the same command
           later to finish it.
           It needs your yes (ticket.py approve) and plays from its time. No
           new entry outside NSE hours or the no_entries_before /
           no_entries_after window, and none with "broker": "kite".
  run --approved-at T
           a REPLAY of a past session as if the yes came at T: it writes only
           nse/state/paper-replay.json (next to --state), never broker.json,
           so nse-risk never counts it. Nothing needs a yes because nothing
           is traded, not even on paper.
  status   lists open paper positions.

    python3 paper.py run --group DEMO-2026-10-27-1
    python3 paper.py run --group DEMO-2026-10-27-1 --approved-at "2026-10-27 09:35" --offline bars/   # replay
    python3 paper.py status

--offline DIR reads DIR/<SYMBOL>.json (a saved Yahoo chart) and never touches
the network. Times are IST. A paper fill is a model: real fills can be worse
(slippage, partial fills, a locked circuit). Not financial advice.
"""

import argparse
import gzip
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal

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

# --- nse charges: begin ---
# Intraday equity charges (Zerodha-style). Shared by the charge-aware tools and kept identical by
# tests/sync_common.py. Edit it in nse-exit/exit.py only. Override key by key under "charges" in config.

# rates checked against zerodha.com/charges on 2026-10-28
CHARGES_DEFAULT = {
    "brokerage_flat": 20,       # rupees per executed order
    "brokerage_pct": 0.03,      # percent of the side's value; the lower of the two applies
    "stt_sell_pct": 0.025,      # percent, sell side
    "txn_pct": 0.00307,         # NSE transaction charge, percent, both sides
    "sebi_per_crore": 10,       # rupees per crore of turnover, both sides
    "stamp_buy_pct": 0.003,     # percent, buy side
    "gst_pct": 18,              # percent of brokerage + txn + SEBI
}


def _cd(x):
    return x if isinstance(x, _Dec) else _Dec(str(x))


def _paisa(x):
    return _cd(x).quantize(_Dec("0.01"), rounding=_HALF_UP)


def charges(buy_value, sell_value, cfg_charges=None):
    """Round-trip charges in rupees as Decimals: brokerage, stt, txn, sebi, stamp, gst, total.
    Each component rounds half up to the paisa before summing; GST is on the rounded components."""
    c = dict(CHARGES_DEFAULT)
    c.update(cfg_charges or {})
    buy, sell = _cd(buy_value), _cd(sell_value)
    pct = _Dec(100)

    def brokerage(v):
        if v <= 0:
            return _Dec("0.00")
        return _paisa(min(_cd(c["brokerage_flat"]), _cd(c["brokerage_pct"]) * v / pct))

    out = {
        "brokerage": brokerage(buy) + brokerage(sell),
        "stt": _paisa(_cd(c["stt_sell_pct"]) * sell / pct),
        "txn": _paisa(_cd(c["txn_pct"]) * buy / pct) + _paisa(_cd(c["txn_pct"]) * sell / pct),
        "sebi": _paisa(_cd(c["sebi_per_crore"]) * buy / _Dec(10000000)) + _paisa(_cd(c["sebi_per_crore"]) * sell / _Dec(10000000)),
        "stamp": _paisa(_cd(c["stamp_buy_pct"]) * buy / pct),
    }
    out["gst"] = _paisa(_cd(c["gst_pct"]) * (out["brokerage"] + out["txn"] + out["sebi"]) / pct)
    out["total"] = sum(out.values(), _Dec("0.00"))
    return out


def round_trip(qty, entry, exit_, cfg=None):
    """Total charges for buying qty at entry and selling at exit_."""
    return charges(_cd(qty) * _cd(entry), _cd(qty) * _cd(exit_), cfg)["total"]
# --- nse charges: end ---

UA = "Mozilla/5.0 (nse-day-trader-skill)"
HOSTS = ("query1", "query2")
PAUSE = 0.25
BAR = 5  # minutes
EXIT_LEGS = ("stop", "target", "square-off")


ENTRY_WINDOW = ("09:30", "14:45")   # nse-risk's defaults for no_entries_before / no_entries_after
REPLAY_FILE = "paper-replay.json"


def real_now():
    """The real clock, aware UTC (tests pin it). A recorded yes is only played on the day it was given."""
    return _dt.datetime.now(_UTC).replace(microsecond=0)


def _same_file(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


class PaperError(Exception):
    pass


def broker_mode(cfg):
    """"paper" or "kite". LIVE is "broker": "kite"; "paper" must agree with it, or it is a config error."""
    cfg = cfg or {}
    b = str(cfg.get("broker") or "paper").strip().lower()
    if b not in ("paper", "kite"):
        raise PaperError("config \"broker\" must be \"paper\" or \"kite\", not %r" % cfg.get("broker"))
    if cfg.get("paper") is not None and bool(cfg.get("paper")) != (b == "paper"):
        raise PaperError("config error: \"broker\" is \"%s\" but \"paper\" is %s. Set both: \"broker\": \"kite\" "
                         "with \"paper\": false for a live account, or \"broker\": \"paper\" with \"paper\": true"
                         % (b, "true" if cfg.get("paper") else "false"))
    return b


def _hhmm(value, key):
    if value in (None, "", False):
        return None
    m = _re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", str(value).strip())
    if not m:
        raise PaperError("%s must be an IST time like \"09:30\", got %r" % (key, value))
    return _dt.time(int(m.group(1)), int(m.group(2)))


def entry_window(now_utc, cfg):
    """None when a new entry may go in at now_utc, else why not (nse-risk's rules: NSE open, and
    outside a special session inside no_entries_before / no_entries_after)."""
    cfg = cfg or {}
    st = market_status(now_utc, cfg)
    local = to_ist(now_utc)
    if st["state"] != "open":
        return "NSE is closed (%s, %s IST)" % (st["reason"] or "closed", local.strftime("%a %Y-%m-%d %H:%M"))
    if _special(local.date(), cfg):
        return None
    t = local.time().replace(second=0, microsecond=0)
    before = _hhmm(cfg.get("no_entries_before", ENTRY_WINDOW[0]), "no_entries_before")
    after = _hhmm(cfg.get("no_entries_after", ENTRY_WINDOW[1]), "no_entries_after")
    if before is not None and t < before:
        return "no new entries before %s IST (it is %s)" % (before.strftime("%H:%M"), t.strftime("%H:%M"))
    if after is not None and t > after:
        return "no new entries after %s IST (it is %s)" % (after.strftime("%H:%M"), t.strftime("%H:%M"))
    return None


def entry_until(day, cfg):
    """Aware UTC start of the last bar a new entry may fill in (no_entries_after), or None."""
    cfg = cfg or {}
    if _special(day, cfg):
        return None
    after = _hhmm(cfg.get("no_entries_after", ENTRY_WINDOW[1]), "no_entries_after")
    return ist_to_utc(_dt.datetime.combine(day, after)) if after is not None else None


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise PaperError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PaperError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def load_state(path):
    st = read_json(path, "broker state", required=False) or {}
    if not isinstance(st.get("orders"), list):
        st["orders"] = []
    return st


def fingerprint(group):
    """Same function as nse-risk/check.py and ticket.py."""
    blob = json.dumps(group.get("legs"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- bars

def _open_url(url, timeout=20):
    """The only network call in this tool."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_chart(sym, day):
    """Yahoo 5-minute bars for one IST day."""
    p1 = int(ist_to_utc(_dt.datetime.combine(day, PREOPEN)).timestamp())
    p2 = int(ist_to_utc(_dt.datetime.combine(day, CLOSE)).timestamp()) + 60
    path = "/v8/finance/chart/%s?interval=5m&period1=%d&period2=%d&includePrePost=false" % (yahoo_quote(sym), p1, p2)
    last = "no answer"
    for n, host in enumerate(HOSTS):
        if n:
            time.sleep(PAUSE)
        try:
            return _open_url("https://%s.finance.yahoo.com%s" % (host, path))
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
        except (urllib.error.URLError, OSError) as e:
            last = "network error: %s" % (e,)
    raise PaperError("Yahoo did not answer for %s (%s): try again in a minute, or use --offline" % (sym, last))


def offline_file(path, sym):
    if os.path.isdir(path):
        for name in (sym + ".json", sym + ".json.gz", yahoo_symbol(sym) + ".json", yahoo_symbol(sym) + ".json.gz"):
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        raise PaperError("no offline file for %s in %s (want %s.json)" % (sym, path, sym))
    if not os.path.exists(path):
        raise PaperError("no offline file %s" % path)
    return path


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def parse_bars(text, sym):
    """Yahoo chart JSON v8 -> [{"t": aware UTC bar start, "o", "h", "l", "c": Decimal}], oldest first."""
    try:
        res = json.loads(text)["chart"]["result"]
        r = res[0] or {}
    except (ValueError, KeyError, TypeError, IndexError):
        raise PaperError("Yahoo returned no chart for %s" % sym)
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        bars.append({"t": _dt.datetime.fromtimestamp(int(t), _UTC),
                     "o": _dec(c if o is None else o), "h": _dec(c if h is None else h),
                     "l": _dec(c if lo is None else lo), "c": _dec(c)})
    bars.sort(key=lambda b: b["t"])
    return bars


def get_bars(sym, day, offline=None):
    if offline:
        f = offline_file(offline, sym)
        try:
            text = read_text(f)
        except (OSError, ValueError) as e:
            raise PaperError("could not read %s: %s" % (f, e))
    else:
        text = fetch_chart(sym, day)
    return [b for b in parse_bars(text, sym) if to_ist(b["t"]).date() == day]


# ---------------------------------------------------------------- the group

def role_legs(group):
    out, has_entry = [], False
    for lg in group.get("legs") or []:
        if not isinstance(lg, dict):
            continue
        role = str(lg.get("role") or "").strip().lower()
        typ = str(lg.get("order_type") or "").upper()
        if role not in ("entry", "stop", "target", "exit"):
            if not has_entry and str(lg.get("transaction_type") or "").upper() == "BUY":
                role = "entry"
            elif typ in ("SL", "SL-M"):
                role = "stop"
            elif typ == "LIMIT" and has_entry:
                role = "target"
            else:
                role = "exit"
        has_entry = has_entry or role == "entry"
        out.append((role, lg))
    return out


def legs_of(group):
    d = {}
    for r, lg in role_legs(group):
        d.setdefault(r, lg)
    return d


def _num(x):
    try:
        return _dec(x)
    except Exception:
        return None


def hhmm(dt_utc):
    return to_ist(dt_utc).strftime("%H:%M")


def simulate(group, bars, approved, flat_dt, cfg_charges=None, last_entry=None):
    """Play the group on the day's bars. approved, flat_dt and last_entry (the start of the last bar
    an entry may fill in, or None) are aware UTC.
    Returns {"entry": fill or None, "exit": fill or None, "open": bool, "notes": [...]}."""
    legs = legs_of(group)
    en = legs.get("entry")
    if en is None or str(en.get("transaction_type", "")).upper() != "BUY":
        raise PaperError("the paper engine plays long groups with a BUY entry leg (long side only)")
    qty = int(_dec(en.get("quantity")))
    stop = _num((legs.get("stop") or {}).get("trigger_price")) if legs.get("stop") else None
    target = _num((legs.get("target") or {}).get("price")) if legs.get("target") else None
    limit = _num(en.get("price")) if str(en.get("order_type", "")).upper() == "LIMIT" else None
    step = _dt.timedelta(minutes=BAR)
    out = {"entry": None, "exit": None, "open": False, "notes": [], "qty": qty, "stop": stop, "target": target}
    tradeable = [b for b in bars if b["t"] >= approved and b["t"] + step <= flat_dt]
    covers = any(b["t"] + step >= flat_dt for b in bars)
    i0 = None
    for i, b in enumerate(tradeable):
        if last_entry is not None and b["t"] > last_entry:
            break
        if limit is None:
            out["entry"] = {"price": b["o"], "t": b["t"], "how": "open of the %s bar" % hhmm(b["t"])}
        elif b["o"] <= limit:
            out["entry"] = {"price": b["o"], "t": b["t"], "how": "open of the %s bar (at or under the limit %s)"
                            % (hhmm(b["t"]), inr(limit))}
        elif b["l"] <= limit:
            out["entry"] = {"price": limit, "t": b["t"], "how": "limit %s reached in the %s bar"
                            % (inr(limit), hhmm(b["t"]))}
        if out["entry"]:
            i0 = i
            break
    if out["entry"] is None:
        if last_entry is not None and any(b["t"] > last_entry for b in bars):
            out["notes"].append("the entry window closed at %s IST before the entry filled: nothing was traded"
                                % hhmm(last_entry))
        elif covers:
            out["notes"].append("the entry never filled before flat_by: nothing was traded")
        else:
            out["open"] = True
            out["notes"].append("no 5-minute bar after the approval yet: the entry is still waiting")
        return out
    last = None
    for b in tradeable[i0:]:
        last = b
        s_hit = stop is not None and b["l"] <= stop
        t_hit = target is not None and b["h"] >= target
        when = hhmm(b["t"])
        if s_hit:
            gap = b["o"] < stop
            px = b["o"] if gap else stop
            if gap:
                how = ("the %s bar opened at %s, below the trigger %s (a gap): filled at the open"
                       % (when, inr(b["o"]), inr(stop)))
            else:
                how = "the %s bar's low %s reached the trigger %s" % (when, inr(b["l"]), inr(stop))
            if t_hit:
                how += ("; both the stop and the target were inside the %s bar: the stop is assumed first "
                        "(conservative)" % when)
            out["exit"] = {"leg": "stop", "price": px, "t": b["t"], "time": when, "how": how}
            break
        if t_hit:
            gap = b["o"] > target
            px = b["o"] if gap else target
            how = ("the %s bar opened at %s, above the target %s: filled at the open" % (when, inr(b["o"]), inr(target))
                   if gap else "the %s bar's high %s reached the target %s" % (when, inr(b["h"]), inr(target)))
            out["exit"] = {"leg": "target", "price": px, "t": b["t"], "time": when, "how": how}
            break
    if out["exit"] is None:
        if covers:
            end = last["t"] + step
            how = "close of the %s to %s bar (flat by %s)" % (hhmm(last["t"]), hhmm(end), hhmm(flat_dt))
            if end != flat_dt:
                how += "; the bar ending at %s is missing, so the last bar before it was used" % hhmm(flat_dt)
            out["exit"] = {"leg": "square-off", "price": last["c"], "t": end, "time": hhmm(end), "how": how}
        else:
            out["open"] = True
            out["notes"].append("the bars end at %s, before flat_by %s: the position is still open"
                                % (hhmm(last["t"] + step), hhmm(flat_dt)))
    if out["exit"] is not None:
        e, x = out["entry"]["price"], out["exit"]["price"]
        gross = ((x - e) * qty).quantize(Decimal("0.01"))
        ch = round_trip(qty, e, x, cfg_charges)
        out["exit"].update({"gross": gross, "charges": ch, "net": gross - ch})
    return out


# ---------------------------------------------------------------- commands

# the records that give or take back a yes: only the latest one for a group counts
YES_OR_NO = ("approved", "declined", "denied", "failed")


def _approval_time(a, state, fp):
    if a.approved_at:
        try:
            return parse_now(a.approved_at)
        except ValueError as e:
            raise PaperError(str(e).replace("--now", "--approved-at"))
    found = latest = None
    for r in state["orders"]:
        if r.get("fingerprint") == fp and r.get("status") in YES_OR_NO:
            latest = r
            if r.get("status") == "approved":
                found = r
    if not found:
        raise PaperError("no yes is recorded for this group: run ticket.py approve with the user's yes first "
                         "(or pass --approved-at to replay a past session, which is never counted)")
    if latest is not found:
        raise PaperError("the yes for this group was followed by a \"%s\" record (at %s): a yes that was taken "
                         "back is not played. Ask the user again (ticket.py show, then approve)"
                         % (latest.get("status"), latest.get("at")))
    if str(found.get("broker") or "paper").lower() != "paper":
        raise PaperError("the yes for this group was recorded for %s, not paper: the paper engine does not play it"
                         % found.get("broker"))
    t = str(found.get("at", "")).replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(t)
    except ValueError:
        raise PaperError("the approval record has no usable time (%r)" % found.get("at"))
    return ist_to_utc(dt)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def cmd_run(a):
    doc = read_json(a.orders, "orders file (run nse-budget first)")
    groups = doc.get("orders", []) if isinstance(doc, dict) else doc
    match = [g for g in groups if isinstance(g, dict) and g.get("group") == a.group]
    if not match:
        raise PaperError("no group %s in %s" % (a.group, a.orders))
    group = match[0]
    cfg = read_json(a.config, "config", required=False) or {}
    mode = broker_mode(cfg)
    replay = bool(a.approved_at)
    if not replay and mode == "kite":
        raise PaperError("the config says \"broker\": \"kite\" (a live account): the paper engine only plays paper "
                         "groups. Use --approved-at for a replay that is never counted")
    out_path = (a.replay_state or os.path.join(os.path.dirname(a.state) or ".", REPLAY_FILE)) if replay else a.state
    if replay and any(_same_file(out_path, p) for p in (a.state, os.path.join("nse", "state", "broker.json"))):
        raise PaperError("--replay-state %s is the broker state (broker.json): a replay is never counted, so it "
                         "must go to its own file (default: %s next to --state)" % (out_path, REPLAY_FILE))
    if replay:
        print("REPLAY (--approved-at): a what-if on past bars, written to %s only. Not a trade, not counted by "
              "nse-risk, and nothing goes to broker.json." % out_path)
    state = load_state(out_path)
    fp = fingerprint(group)
    approved = _approval_time(a, state if replay else load_state(a.state), fp)
    local = to_ist(approved)
    if not replay:
        today = to_ist(real_now()).date()
        if local.date() != today:
            raise PaperError("the recorded yes for this group is from %s, not today (%s IST): a yes counts only on "
                             "the day it was given. Ask the user again (ticket.py show, then approve), or replay "
                             "that day with --approved-at" % (local.date().isoformat(), today.isoformat()))
    day = local.date()
    ok, why = trading_day(day, cfg)
    if not ok:
        raise PaperError("NSE is closed on %s (%s): nothing to play" % (day.isoformat(), why))
    ds = day.isoformat()
    mine = [r for r in state["orders"] if r.get("fingerprint") == fp and r.get("date") == ds
            and r.get("broker") == "paper"]
    if any(r.get("status") == "filled" and r.get("leg") in EXIT_LEGS for r in mine):
        raise PaperError("group %s was already played on paper on %s (ticket.py log shows it)" % (a.group, ds))
    plan = group.get("plan") or {}
    fb = str(plan.get("flat_by") or cfg.get("flat_by") or "15:10")
    try:
        flat_t = _dt.time(int(fb[:2]), int(fb[3:5]))
    except (ValueError, IndexError):
        raise PaperError("flat_by must be an IST time like \"15:10\", got %r" % fb)
    flat_dt = ist_to_utc(_dt.datetime.combine(day, flat_t))
    if approved > flat_dt - _dt.timedelta(minutes=BAR):
        raise PaperError("approved at %s IST, after the last bar before flat_by %s: no new paper entry"
                         % (local.strftime("%H:%M"), fb))
    had_entry = [r for r in mine if r.get("status") == "filled" and r.get("leg") == "entry"]
    shut = None if had_entry else entry_window(approved, cfg)
    if shut:
        raise PaperError("approved at %s IST: %s. No new paper entry (exits are not held to this)"
                         % (local.strftime("%H:%M"), shut))
    legs = legs_of(group)
    sym = str(legs.get("entry", {}).get("tradingsymbol") or "").strip().upper()
    bars = get_bars(sym, day, a.offline)
    res = simulate(group, bars, approved, flat_dt, cfg.get("charges"), entry_until(day, cfg))
    qty = res["qty"]
    src = ("offline file in %s" % a.offline) if a.offline else "Yahoo"
    print("%s: %s, %d %s, approved %s IST, 5-minute bars of %s (%s)"
          % ("REPLAY" if replay else "PAPER", a.group, qty, sym, local.strftime("%H:%M"), ds, src))
    ent = res["entry"]
    recorded_at = to_ist(real_now()).isoformat()      # "at" is the bar time; this is when it was written
    if ent is not None:
        print("  entry       BUY %d %s at %s, %s" % (qty, sym, inr(ent["price"]), ent["how"]))
        if not had_entry:
            if res["stop"] is not None:   # measured from the actual fill, charges included
                ml = float((qty * (ent["price"] - res["stop"]) + round_trip(qty, ent["price"], res["stop"],
                                                                            cfg.get("charges"))).quantize(Decimal("0.01")))
            else:
                ml = _f(plan.get("max_loss"))
            base = {"date": ds, "group": a.group, "fingerprint": fp, "symbol": sym, "qty": qty,
                    "broker": "paper", "paper": True, "recorded_at": recorded_at}
            if replay:
                base["replay"] = True
            state["orders"].append(dict(base, at=to_ist(ent["t"]).isoformat(), status="placed", side="BUY",
                                        kind="entry", max_loss=ml, legs=len(role_legs(group)),
                                        stop=_f(res["stop"]), target=_f(res["target"]), flat_by=fb,
                                        plain="paper: %d %s, stop %s, target %s, flat by %s"
                                        % (qty, sym, inr(res["stop"]) if res["stop"] is not None else "none",
                                           inr(res["target"]) if res["target"] is not None else "none", fb)))
            state["orders"].append(dict(base, at=to_ist(ent["t"]).isoformat(), status="filled", leg="entry",
                                        side="BUY", price=float(ent["price"]), time=hhmm(ent["t"]),
                                        plain="paper: BUY %d %s at %s (entry)" % (qty, sym, inr(ent["price"]))))
    ex = res["exit"]
    if ex is not None:
        print("  %-11s SELL %d %s at %s, %s" % (ex["leg"], qty, sym, inr(ex["price"]), ex["how"]))
        print("  P&L         gross %s, charges %s, net %s" % (inr(ex["gross"]), inr(ex["charges"]), inr(ex["net"])))
        state["orders"].append({"at": to_ist(ex["t"]).isoformat(), "date": ds, "status": "filled", "group": a.group,
                                "fingerprint": fp, "leg": ex["leg"], "symbol": sym, "side": "SELL", "qty": qty,
                                "price": float(ex["price"]), "time": ex["time"], "gross_pnl": float(ex["gross"]),
                                "charges": float(ex["charges"]), "net_pnl": float(ex["net"]), "note": ex["how"],
                                "broker": "paper", "paper": True, "replay": replay, "recorded_at": recorded_at,
                                "plain": "paper: SELL %d %s at %s (%s)" % (qty, sym, inr(ex["price"]), ex["leg"])})
    for n in res["notes"]:
        print("  note: " + n)
    if res["open"] and ent is not None:
        print("  The paper position is still open: run this command again later today to finish it.")
    write_json(out_path, state)
    print("Recorded in %s. A paper fill is a model on past bars: real fills can be worse (slippage, partial "
          "fills, a locked circuit). Not financial advice." % out_path)
    return 0


def open_positions(state):
    out = []
    recs = state.get("orders", [])
    for r in recs:
        if r.get("broker") != "paper" or r.get("status") != "filled" or r.get("leg") != "entry":
            continue
        fp, day = r.get("fingerprint"), r.get("date")
        closed = any(x.get("fingerprint") == fp and x.get("date") == day and x.get("status") == "filled"
                     and x.get("leg") in EXIT_LEGS for x in recs)
        if not closed:
            placed = [x for x in recs if x.get("fingerprint") == fp and x.get("date") == day
                      and x.get("status") == "placed"]
            out.append((r, placed[-1] if placed else {}))
    return out


def cmd_status(a):
    state = load_state(a.state)
    rows = open_positions(state)
    if not rows:
        print("No open paper positions.")
        return 0
    for r, p in rows:
        stop = inr(p["stop"]) if p.get("stop") is not None else "none"
        target = inr(p["target"]) if p.get("target") is not None else "none"
        print("OPEN  %s: %d %s bought at %s at %s IST on %s; stop %s, target %s, flat by %s"
              % (r.get("group"), int(r.get("qty") or 0), r.get("symbol"), inr(r.get("price")), r.get("time"),
                 r.get("date"), stop, target, p.get("flat_by", "15:10")))
    print("%d open paper position%s. Finish each with paper.py run --group G." % (len(rows), "" if len(rows) == 1
                                                                                   else "s"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Local paper engine (nse-broker). Never sends an order anywhere.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run", help="play an approved group on 5-minute bars")
    p.add_argument("--group", required=True)
    p.add_argument("--orders", default="nse/orders.json")
    p.add_argument("--config", default="nse/config.json")
    p.add_argument("--state", default="nse/state/broker.json")
    p.add_argument("--offline", metavar="DIR", help="saved Yahoo chart JSON (DIR/<SYMBOL>.json or one file)")
    p.add_argument("--approved-at", help="REPLAY as if the yes came at this IST time, e.g. \"2026-10-27 09:35\"; "
                   "writes only the replay file, never broker.json (default: play the recorded yes)")
    p.add_argument("--replay-state", help="the replay file (default: paper-replay.json next to --state)")
    p = sub.add_parser("status", help="open paper positions (--state nse/state/paper-replay.json for replays)")
    p.add_argument("--state", default="nse/state/broker.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "status": cmd_status}[a.cmd](a)
    except PaperError as e:
        print("paper.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
