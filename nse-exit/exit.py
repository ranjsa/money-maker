#!/usr/bin/env python3
"""Exit Planner for nse-exit: sets the stop and the target BEFORE you enter an
NSE stock, so you know when to get out. Standard library only. Long side only,
intraday (MIS). It never places an order.

    python3 exit.py plan                                    # from nse/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 2R --write  # the demo: entry 1015.00 -> stop 1005.20, target 1034.60
    python3 exit.py plan --symbol DEMO --entry 1015 --or-high 1012.40 --or-low 998.10
    python3 exit.py plan --stop atr:1.5 --offline DEMO.json # ATR needs the session's bars
    python3 exit.py plan --symbol TATASTEEL --entry 150.40 --setup level --stop pct:1
    python3 exit.py show                                    # the current nse/plan.json

Stops (a long's stop rounds DOWN to the NSE tick, away from the entry):
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  price:X    your price
  pct:X      X percent under the entry
  atr:K      K times the 14-bar ATR under the entry, on the session's bars up
             to the entry bar (--offline FILE or Yahoo). With fewer than 14
             bars by then it averages all of them and says so.
  A level setup has no opening-range default: give a stop.

Targets (rounded to the nearest tick, but never toward the entry):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop
  price:X                      your price

The tick comes from the ENTRY price: below 250 it is 0.01, 250 to 1000 0.05,
over 1000 to 5000 0.10, and so on up to 5.00 over 20000.

Charges. A day trade pays brokerage, STT, exchange and SEBI fees, stamp duty
and GST both ways. The plan estimates them per share (for CHARGE_REF_QTY = 100
shares; nse-budget recomputes them exactly at the real quantity) and prints the
net reward:risk after them. It refuses a plan whose charges are over half the
risk a share: widen the stop or skip.

Refused, with the reason: a stop at or above the entry, a target at or below
it, a stop under 3 ticks from the entry (too tight to survive the spread),
charges over half the risk, an entry.json that has not triggered (unless you
give --entry), a short (v1 plans longs only).

flat_by (default 15:10 IST, or "flat_by" in nse/config.json): out by then, win
or lose. Brokers may auto square-off MIS from about 15:20, so a later flat-by
only gets a warning. "min_reward_risk" in the config (default 1.0) only warns
here; nse-risk enforces it.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
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
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

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
import decimal as _decimal

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
BACKOFF = 5.0
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"flat_by": "15:10", "min_reward_risk": 1.0}
ATR_BARS = 14
MIN_TICKS = 3                      # a stop closer than 3 ticks is inside the spread's noise
CHARGE_REF_QTY = 100               # shares used to turn round-trip charges into a per-share estimate
CHARGE_RISK_SHARE = Decimal("0.5") # refuse when charges per share are over this fraction of the risk
SQUARE_OFF = datetime.time(15, 20) # brokers may auto square-off MIS from about here
PAISA = Decimal("0.01")
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
NOTE = "A stop is a plan, not a guarantee: a gap or a fast market can fill past it."


class PlanError(Exception):
    pass


# ---------------------------------------------------------------- time (IST; the clock itself is in the common block)

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def utc_of(ts):
    """Unix seconds -> aware UTC."""
    return EPOCH + datetime.timedelta(seconds=int(ts))


def local_of(utc):
    """Aware UTC -> naive IST wall clock."""
    return to_ist(utc).replace(tzinfo=None)


def now_arg(text):
    try:
        return parse_now(text)
    except ValueError as e:
        raise PlanError(str(e))


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise PlanError("%s wants an IST time like 15:10, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def parse_clock(text, what):
    """09:35 AM, 9:35, 2:15 PM, 14:15 -> time."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*(?:IST)?\s*$", str(text))
    if not m:
        raise PlanError("%s wants a time like 09:35 AM, not %r" % (what, text))
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        if not 1 <= h <= 12:
            raise PlanError("%s wants a time like 09:35 AM, not %r" % (what, text))
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    if h > 23 or mi > 59:
        raise PlanError("%s wants a time like 09:35 AM, not %r" % (what, text))
    return datetime.time(h, mi)


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def ampm(t):
    """09:35 AM, 02:15 PM: the form entry.json uses."""
    return "%02d:%02d %s" % (t.hour % 12 or 12, t.minute, "AM" if t.hour < 12 else "PM")


def calendar_warning(d, cfg=None):
    """'' or the one-line warning for a year the holiday list does not hold."""
    if trading_day(d, cfg)[1] == "calendar not loaded":
        return ("NSE calendar for %d not loaded: run nse-alerts/alerts.py calendar --refresh or check nseindia.com "
                "(treating Mon-Fri as open)" % d.year)
    return ""


# ---------------------------------------------------------------- money math (Decimal, tick-rounded)

def D(x, what="a number"):
    try:
        if isinstance(x, float):
            return Decimal(repr(x))
        return Decimal(str(x).strip().lstrip("₹").replace(",", ""))
    except (InvalidOperation, ValueError):
        raise PlanError("%s is not a number: %r" % (what, x))


def paisa(x):
    """Round half up to the paisa."""
    return D(x).quantize(PAISA, rounding=ROUND_HALF_UP)


def num(x):
    """1, 1.5, 2.25: a Decimal without trailing zeros."""
    return format(D(x).normalize(), "f")


def ratio(x):
    """reward:risk for people: 1.0, 1.5, 1.37."""
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    m = re.match(r"^(price|pct|atr):\s*₹?([0-9][0-9,]*\.?[0-9]*)$", t)
    if not m:
        raise PlanError("--stop is or-mid, or-low, price:X, pct:X or atr:K (got %r)" % text)
    kind, val = m.group(1), D(m.group(2), "--stop")
    if val <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop pct:X needs X under 100")
    return kind, val


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = D(m.group(1), "--target")
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(r"^price:\s*₹?([0-9][0-9,]*\.?[0-9]*)$", t.lower())
    if m:
        return "price", D(m.group(1), "--target")
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def r_label(r):
    n = int(r) if r == r.to_integral_value() else None
    return "%sR (%s times the risk)" % (num(r), WORDS.get(n, num(r)))


def est_charges_per_share(entry, target, cfg_charges=None):
    """Round-trip charges for CHARGE_REF_QTY shares, per share, to the paisa."""
    total = round_trip(CHARGE_REF_QTY, entry, target, cfg_charges)
    return (total / CHARGE_REF_QTY).quantize(PAISA, rounding=ROUND_HALF_UP)


def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_n=None, cfg_charges=None):
    """The stop, the target, the risk and the charges, in exact paise. Raises PlanError with the reason."""
    e = paisa(entry)
    if e <= 0:
        raise PlanError("the entry price must be above ₹0")
    tick = tick_for(e)
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)" % stop_rule.replace("_", "-"))
        oh, ol = paisa(or_high), paisa(or_low)
        if oh <= ol:
            raise PlanError("the opening-range high %s must be above its low %s" % (inr(oh), inr(ol)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = %s" % (inr(oh), inr(ol), inr(raw, 3).rstrip("0").rstrip("."))
        else:
            raw, label = ol, "opening-range low"
    elif stop_rule == "price":
        raw, label = stop_param, "your price"
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100)
        label = "%s%% under the entry" % num(stop_param)
        detail = "%s x (1 - %s%%) = ₹%s" % (inr(e), num(stop_param), num(raw))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the session's bars")
        a = D(round(atr, 6))
        raw = e - stop_param * a
        label = "%s x ATR(%d) under the entry" % (num(stop_param), atr_n)
        detail = "ATR ₹%s over the %d bars to the entry%s" % (
            num(a.quantize(Decimal("0.0001"))), atr_n, " (fewer than 14 so far)" if atr_n < ATR_BARS else "")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)
    stop = floor_tick(raw, e)
    if stop != D(raw):
        detail = (detail + ", rounded down to the %s tick" % format(tick, "f") if detail
                  else "%s rounded down to the %s tick" % (inr(raw, 3).rstrip("0").rstrip("."), format(tick, "f")))
    if stop >= e:
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (inr(stop), label, inr(e)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above ₹0" % inr(stop))
    risk = e - stop
    if risk < MIN_TICKS * tick:
        raise PlanError("the stop is %s under the entry, less than %d ticks (%s at the %s tick on %s): too tight to "
                        "survive the spread, normal wiggle would stop you out. Widen it to at least %d ticks."
                        % (inr(risk), MIN_TICKS, inr(MIN_TICKS * tick), format(tick, "f"), inr(e), MIN_TICKS))
    if target_rule == "r":
        exact = e + target_param * risk
        t_label = r_label(target_param)
    else:
        exact = target_param
        t_label = None
    target = round_tick(exact, e)
    if target < exact:                       # never round toward the entry
        target = ceil_tick(exact, e)
    if target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (inr(target), inr(e)))
    rr = ((target - e) / risk).quantize(PAISA, rounding=ROUND_HALF_UP)
    if t_label is None:
        t_label = "your price (%sR)" % ratio(rr)
    ec = est_charges_per_share(e, target, cfg_charges)
    if ec > CHARGE_RISK_SHARE * risk:
        raise PlanError("charges ≈ %s/share are over half the %s risk/share: widen the stop or skip"
                        % (inr(ec), inr(risk)))
    net = ((target - e - ec) / (risk + ec)).quantize(PAISA, rounding=ROUND_HALF_UP)
    return {"entry": e, "tick": tick, "stop": stop, "stop_label": label, "stop_detail": detail, "target": target,
            "target_r": target_param if target_rule == "r" else rr, "target_label": t_label,
            "risk": risk, "reward_risk": rr, "est_charges_per_share": ec, "net_reward_risk": net}


# ---------------------------------------------------------------- bars for ATR (same reader as nse-entry)

def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """query2 first, query1 on 429 or 5xx; one retry after a pause on 429."""
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown ticker
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise PlanError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise PlanError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise PlanError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_chart(sym, interval, day, offline=None):
    ysym = yahoo_symbol(sym)
    if offline:
        path = offline
        if os.path.isdir(offline):
            base = str(sym).upper()
            for name in (base + ".json", base + ".json.gz", ysym + ".json", ysym + ".json.gz"):
                path = os.path.join(offline, name)
                if os.path.exists(path):
                    break
        if not os.path.exists(path):
            raise PlanError("no offline file %s" % path)
        try:
            text = read_text(path)
        except (OSError, ValueError) as e:
            raise PlanError("could not read %s: %s" % (path, e))
    else:
        back = (local_of(utc_now()).date() - day).days
        rng = "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        if interval == "1m" and back > 6:
            raise PlanError("Yahoo keeps 1-minute bars for about a week")
        text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=%s" % (yahoo_quote(sym), rng, interval))
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise PlanError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise PlanError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown ticker"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    if str(meta.get("symbol") or ysym).upper() != ysym:
        raise PlanError("the chart file holds %s, not %s" % (meta.get("symbol"), sym))
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        h, lo = at("high", i), at("low", i)
        bars.append({"t": int(t), "h": float(c if h is None else h), "l": float(c if lo is None else lo),
                     "c": float(c)})
    return {"bars": bars, "data_time": meta.get("regularMarketTime")}


def session_bars(chart, day, interval, upto_slot=None, now_utc=None, cfg=None):
    """Closed, on-grid bars of one NSE session (IST), oldest first (the live tick is dropped)."""
    step = INTERVALS[interval]
    s = session(day, cfg)
    if not s:
        return []
    open_dt = datetime.datetime.combine(day, s[0])
    close_dt = datetime.datetime.combine(day, s[1])
    if now_utc is None and chart.get("data_time"):
        now_utc = utc_of(chart["data_time"])
    local = local_of(now_utc) if now_utc else None
    out = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue
        lt = local_of(utc_of(b["t"]))
        if lt.date() != day or not (open_dt <= lt < close_dt):
            continue
        if local and lt + datetime.timedelta(seconds=step) > local:
            continue
        slot = int((lt - open_dt).total_seconds()) // step
        if upto_slot is not None and slot > upto_slot:
            continue
        out[slot] = b
    return [out[k] for k in sorted(out)]


def atr_of(bars, n=ATR_BARS):
    """Average true range of the last n bars (all of them if fewer). The first
    bar of the session uses its own high - low (no overnight gap)."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- plan

def read_json(path, what):
    if not path or not os.path.exists(path):
        raise PlanError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PlanError("%s is not valid JSON: %s" % (path, e))


def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        if not isinstance(doc, dict):
            raise PlanError("%s is not valid JSON: expected an object" % path)
        for k in DEFAULTS:
            if doc.get(k) is not None:
                cfg[k] = doc[k]
        if isinstance(doc.get("special_sessions"), dict):
            cfg["special_sessions"] = doc["special_sessions"]
        if isinstance(doc.get("charges"), dict):
            cfg["charges"] = doc["charges"]
    return cfg


def resolve_flat_by(day, explicit, cfg, entry_time):
    """(HH:MM, warning). Refuses a flat-by at or after the close; warns after the broker square-off."""
    t = parse_hhmm(explicit or cfg.get("flat_by") or DEFAULTS["flat_by"], "--flat-by" if explicit else "flat_by")
    s = session(day, cfg) if day else None
    warn = ""
    if s:
        if t >= s[1]:
            raise PlanError("%s %s is at or after the %s close on %s" % (
                "--flat-by" if explicit else "flat_by", hm(t), hm(s[1]), day))
        if t <= s[0]:
            raise PlanError("%s %s is at or before the %s open on %s" % (
                "--flat-by" if explicit else "flat_by", hm(t), hm(s[0]), day))
    if t > SQUARE_OFF:
        warn = ("Flat-by %s is late: after %s your broker may auto square-off MIS at its own price, with its own "
                "fee." % (hm(t), hm(SQUARE_OFF)))
    if entry_time and entry_time >= t:
        raise PlanError("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                        % (ampm(entry_time), hm(t)))
    return hm(t), warn


def gather(a):
    """The entry facts, from nse/entry.json or the flags."""
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    if a.side in ("sell", "short"):
        raise PlanError("v1 plans longs only: no stops above a short entry. Plan a buy instead.")
    if a.symbol:
        if a.entry is None:
            raise PlanError("--symbol needs --entry PRICE")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("give both --or-high and --or-low, or neither")
        info = {"symbol": a.symbol.upper(), "entry": a.entry, "setup": a.setup or ("orb" if a.or_high is not None else "level"),
                "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "source": "flags"}
    else:
        path = a.from_path or "nse/entry.json"
        doc = read_json(path, "entry (run /nse-entry first, or give --symbol and --entry)")
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise PlanError("%s is a %s: v1 plans longs only" % (path, doc.get("side")))
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to plan ahead"
                            % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        orr = doc.get("opening_range") or {}
        info = {"symbol": str(doc.get("symbol") or "").upper(), "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or (doc.get("time") if a.entry is None else None),
                "entry_bar": a.entry_bar if a.entry_bar is not None else (doc.get("bar_index") if a.entry is None else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes, "source": path}
        if not info["symbol"] or info["entry"] is None:
            raise PlanError("%s has no symbol or price" % path)
    if info["interval"] not in INTERVALS:
        raise PlanError("interval is 5m or 1m")
    return info


def build_plan(a, now_utc):
    info = gather(a)
    cfg = read_config(a.config)
    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop price:X, pct:X or atr:K"
                            % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(a.target or "2R")
    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise PlanError("date wants YYYY-MM-DD, not %r" % info["date"])
    else:
        day = local_of(now_utc).date()
    entry_time = parse_clock(info["entry_time"], "the entry time") if info["entry_time"] else None
    atr = atr_n = None
    if stop_rule == "atr":
        chart = load_chart(info["symbol"], info["interval"], day, a.offline)
        bars = session_bars(chart, day, info["interval"], info["entry_bar"], now_arg(a.now) if a.now else None, cfg)
        if len(bars) < 3:
            raise PlanError("an atr stop needs at least 3 closed %s bars of %s on %s by the entry; found %d"
                            % (info["interval"], info["symbol"], day, len(bars)))
        atr, atr_n = atr_of(bars)
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["or_high"], info["or_low"],
                atr, atr_n, cfg.get("charges"))
    flat_by, flat_warn = resolve_flat_by(day, a.flat_by, cfg, entry_time)
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(paisa(info["or_high"])), "low": float(paisa(info["or_low"])),
               "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "nse-exit", "symbol": info["symbol"], "side": "buy", "date": day.isoformat(),
            "setup": info["setup"], "entry": float(c["entry"]),
            "entry_time": ampm(entry_time) if entry_time else None,
            "entry_bar": info["entry_bar"], "interval": info["interval"], "opening_range": orr,
            "stop": float(c["stop"]), "stop_rule": stop_rule, "stop_label": c["stop_label"],
            "target": float(c["target"]), "target_r": float(c["target_r"]), "target_label": c["target_label"],
            "risk_per_share": float(c["risk"]), "reward_risk": float(c["reward_risk"]), "flat_by": flat_by,
            "tick": format(c["tick"], "f"),
            "est_charges_per_share": float(c["est_charges_per_share"]),
            "net_reward_risk": float(c["net_reward_risk"]),
            "exchange": "NSE", "product": "MIS",
            "stop_detail": c["stop_detail"], "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "note": NOTE}
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    warnings = []
    if flat_warn:
        warnings.append(flat_warn)
    mrr = cfg.get("min_reward_risk")
    try:
        if mrr is not None and c["reward_risk"] < D(mrr):
            warnings.append("Reward:risk %s is under your min_reward_risk %s in the config: nse-risk checks that."
                            % (ratio(c["reward_risk"]), num(D(mrr))))
        elif mrr is not None and c["net_reward_risk"] < D(mrr):
            warnings.append("Net reward:risk %s after charges is under your min_reward_risk %s in the config."
                            % (ratio(c["net_reward_risk"]), num(D(mrr))))
    except PlanError:
        pass
    ok, why = trading_day(day, cfg)
    if not ok:
        warnings.append("Market closed on %s: %s." % (day.isoformat(), why))
    cal = calendar_warning(day, cfg)
    if cal:
        warnings.append(cal)
    today = local_of(now_utc).date()
    if day != today:
        warnings.append("This plan is for %s, not today (%s)." % (day.isoformat(), today.isoformat()))
    return plan, warnings


def describe(plan):
    day = datetime.date.fromisoformat(plan["date"])
    setup = {"orb": "opening-range breakout", "level": "close above a level"}.get(plan["setup"], plan["setup"])
    e = D(plan["entry"])
    risk = D(plan["risk_per_share"])
    pct = (risk / e * 100).quantize(PAISA, rounding=ROUND_HALF_UP)
    when = ""
    if plan.get("entry_time"):
        when = " at %s IST" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (bar %d)" % plan["entry_bar"]
    out = ["Exit plan: %s long (NSE, MIS), %s, %s on %s bars" % (
        plan["symbol"], day.strftime("%a %Y-%m-%d"), setup, "5-minute" if plan["interval"] == "5m" else "1-minute")]
    out.append("  Entry         %s%s" % (inr(plan["entry"]), when))
    out.append("  Stop          %s  %s%s" % (inr(plan["stop"]), plan["stop_label"],
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (inr(plan["target"]), plan["target_label"]))
    out.append("  Risk a share  %s (%s%% of the entry, tick %s)" % (inr(plan["risk_per_share"]), pct,
                                                                   inr(plan.get("tick") or "0.01")))
    out.append("  Reward:risk   %s" % ratio(plan["reward_risk"]))
    if plan.get("est_charges_per_share") is not None:
        out.append("  Charges       about %s a share round trip (at %d shares)" % (
            inr(plan["est_charges_per_share"]), CHARGE_REF_QTY))
    if plan.get("net_reward_risk") is not None:
        out.append("  Net R         %s after charges" % ratio(plan["net_reward_risk"]))
    out.append("  Flat by       %s IST, win or lose: no overnight hold" % plan["flat_by"])
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(NOTE)
    return out


def cmd_plan(a):
    now = now_arg(a.now)
    plan, warnings = build_plan(a, now)
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print("Note: " + w)
    if a.write:
        print("Wrote %s." % a.write)
    print("Next: /nse-timemachine replays it, then /nse-budget sizes it to your max loss. Nothing was placed.")
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % a.plan)
        return 1
    plan = read_json(a.plan, "plan")
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(k) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2))
        return 0
    for line in describe(dict({"setup": "orb", "interval": "5m", "stop_label": "", "target_label": ""}, **plan)):
        print(line)
    today = local_of(now_arg(a.now)).date().isoformat()
    if plan["date"] != today:
        print("Note: this plan is for %s, not today (%s)." % (plan["date"], today))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exit Planner (nse-exit): the stop and target before the entry.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop and target")
    p.add_argument("--from", dest="from_path", metavar="PATH", help="entry.json to plan from (default nse/entry.json)")
    p.add_argument("--symbol")
    p.add_argument("--entry", type=float, help="entry price (or a plan-ahead price with --from)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level"])
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today)")
    p.add_argument("--time", help="entry time, e.g. 09:35 AM")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["5m", "1m"])
    p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="v1: buy only")
    p.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    p.add_argument("--flat-by", help="HH:MM IST (default: config flat_by, 15:10)")
    p.add_argument("--config", default="nse/config.json")
    p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON (file or folder) for an atr stop")
    p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-27 09:36"')
    p.add_argument("--write", nargs="?", const="nse/plan.json", metavar="PATH",
                   help="save the plan (default nse/plan.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default="nse/plan.json")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except PlanError as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
