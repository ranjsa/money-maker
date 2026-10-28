#!/usr/bin/env python3
"""Pre-trade risk check for nse-risk. Standard library only; the network only for the price bands.

Reads the proposed order groups (nse/orders.json, three Kite legs each: entry,
stop-loss, target) and checks every group against your rules (nse/config.json),
your account (nse/portfolio.json), prices (nse/prices.json), what was placed
today (nse/state/broker.json) and NSE's price bands (nse/state/bands.json, a
cache of sec_list.csv). Each group gets PASS or BLOCK with the reasons. Groups
are checked in file order as if each PASS had filled, so buys that are fine one
by one but break a limit together are caught.

    python3 check.py                                 # the nse/ files, writes nse/state/risk.json
    python3 check.py --refresh-bands                 # fetch today's sec_list.csv first (once a day)
    python3 check.py --now "2026-10-27 10:15"        # pin the clock (IST)
    python3 check.py --orders nse/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Entries and exits. An ENTRY group opens or adds to a position (a BUY entry leg,
or a sell of more than you hold). An EXIT group only reduces what you hold
(SELL legs; a stop and a target for the same shares count once). Getting out
must always be possible: an exit is checked for format, F&O, series and short
sales only; every other rule turns into a warning for it.

What blocks a group (every group):

  format        no legs, legs on different symbols or exchanges, a side that is
                not BUY / SELL, a quantity that is not a whole number of shares,
                an unknown order_type, variety or validity, a LIMIT / SL leg
                without a price, an SL / SL-M leg without a trigger_price
  fno           an NFO / BFO leg unless "allow_fno": true
  series        the stock is not in the EQ series (BE, BZ, SM... are
                trade-to-trade or SME: no intraday). From the price-band file,
                else nse/universe.json; a symbol in neither is blocked (for an
                exit: a warning); only with neither file loaded is it a warning
  short         selling more than you hold, unless "allow_short": true

What blocks an entry (on top):

  legs          no stop leg while "require_stop": true; legs with different
                quantities; a stop leg that is not SL-M or SL; a product other
                than MIS (this is a day trade); an exchange other than NSE / BSE;
                a stop trigger at or above the entry, a target at or below it
  circuit       the entry within 3 ticks of the upper band (previous close x
                (1 + band%)); a 2% or 5% band stock unless "allow_banded": true;
                price bands missing or older than today (one live refresh is
                tried first, never with --offline); no previous close from
                today for a banded stock. A stock missing from a fresh band
                file is a warning
  blocked       a symbol on your "blocked" list
  size          under min_order_inr, over max_order_inr
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer
  leverage      the buy costs more than your cash (MIS margin is not modelled),
                unless "allow_leverage": true
  duplicate     the same symbol and side twice in this batch, or already
                placed today (nse/state/broker.json)
  daily limit   max_orders_per_day Kite orders (legs) placed today
  loss          qty x (entry - stop) + round-trip charges at the stop over
                max_loss_per_trade_inr. The entry is the limit price for a LIMIT
                entry, else the HIGHER of the plan's entry and a fresh price
                (under 15 minutes old), so a stale plan cannot understate the risk
  reward:risk   (target - entry) / (entry - stop) under min_reward_risk
  daily loss    the max losses at the stops of today's entries plus this one
                over max_daily_loss_inr (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before or after no_entries_after (IST), or NSE
                is closed (weekend, NSE holiday, pre-open, outside 09:15 to 15:30).
                A special session in "special_sessions" counts as open in its times

Exit code 0 when every group passes, 1 when any group is blocked.
The verdicts go to nse/state/risk.json keyed by each group's fingerprint (the
sha256 of its legs), so nse-broker only shows a group as ready if THIS exact
group passed today. Education and tooling, not financial advice.
"""

import argparse
import datetime
import csv
import hashlib
import io
import json
import os
import re
import sys
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

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 80000,
    "min_order_inr": 500, "max_loss_per_trade_inr": 1000, "max_daily_loss_inr": 3000, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:10",
    "product": "MIS", "exchange": "NSE", "paper": True, "blocked": [],
    "allow_leverage": False, "allow_short": False, "allow_fno": False, "allow_banded": False,
    "special_sessions": {}, "charges": {},
}
SEC_LIST_URL = "https://nsearchives.nseindia.com/content/equities/sec_list.csv"
UA = "Mozilla/5.0 (nse-day-trader-skill)"
ORDER_TYPES = ("MARKET", "LIMIT", "SL", "SL-M")
VARIETIES = ("regular", "amo", "co", "iceberg", "auction")
VALIDITIES = ("DAY", "IOC", "TTL")
EQUITY_EXCHANGES = ("NSE", "BSE")
FNO_EXCHANGES = ("NFO", "BFO")
TIGHT_BANDS = (2.0, 5.0)
CIRCUIT_TICKS = 3
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
PAISA = Decimal("0.01")
FRESH_MINUTES = 15
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "trigger": "the trigger price", "stale price": "an old price"}


class RiskError(Exception):
    pass


# ---------------------------------------------------------------- time (IST)

def parse_utc(text):
    """An ISO time -> aware UTC datetime, or None. A time without an offset is taken as UTC."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=datetime.timezone.utc) if dt.tzinfo is None else dt


def hhmm(value, key):
    """"09:30" -> time(9, 30); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be an IST time like \"09:30\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def calendar_warning(d, cfg=None):
    """'' or the one-line warning for a year the holiday list does not hold."""
    if trading_day(d, cfg)[1] == "calendar not loaded":
        return ("NSE calendar for %d not loaded: run nse-alerts/alerts.py calendar --refresh or check nseindia.com "
                "(treating Mon-Fri as open)" % d.year)
    return ""


def window_check(now_utc, c):
    """(reasons, warnings) for an entry now. Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return [], []
    local = to_ist(now_utc)
    d, t = local.date(), local.time().replace(second=0, microsecond=0)
    stamp = local.strftime("%a %Y-%m-%d %H:%M")
    ok, why = trading_day(d, c)
    st = market_status(now_utc, c)
    nxt = st["next_open"].replace("T", " ")[:16] + " IST" if st["next_open"] else "unknown"
    if not ok:
        return ["window: NSE is closed today (%s, %s IST); next open %s" % (why, stamp, nxt)], []
    if st["state"] != "open":
        o, cl = session(d, c)
        return ["window: NSE is closed (%s, %s IST; the session is %s to %s); next open %s"
                % (st["reason"], stamp, o.strftime("%H:%M"), cl.strftime("%H:%M"), nxt)], []
    sp = _special(d, c)
    if sp:
        return [], ["special session today, %s to %s IST: your no_entries_before / no_entries_after window does "
                    "not apply; be flat before %s IST" % (sp[0].strftime("%H:%M"), sp[1].strftime("%H:%M"),
                                                          sp[1].strftime("%H:%M"))]
    if before is not None and t < before:
        return ["window: no new entries before %s IST (it is %s)" % (before.strftime("%H:%M"),
                                                                    t.strftime("%H:%M"))], []
    if after is not None and t > after:
        return ["window: no new entries after %s IST (it is %s)" % (after.strftime("%H:%M"),
                                                                   t.strftime("%H:%M"))], []
    return [], []


def broker_mode(cfg):
    """"paper" or "kite" from the config. LIVE is "broker": "kite"; "paper" must agree with it."""
    cfg = cfg or {}
    b = str(cfg.get("broker") or "paper").strip().lower()
    if b not in ("paper", "kite"):
        raise RiskError("config \"broker\" must be \"paper\" or \"kite\", not %r" % cfg.get("broker"))
    if cfg.get("paper") is not None and bool(cfg.get("paper")) != (b == "paper"):
        raise RiskError("config error: \"broker\" is \"%s\" but \"paper\" is %s. Set both: \"broker\": \"kite\" "
                        "with \"paper\": false for a live account, or \"broker\": \"paper\" with \"paper\": true"
                        % (b, "true" if cfg.get("paper") else "false"))
    return b


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def fingerprint(group):
    """sha256 of the group's legs as canonical JSON: the exact legs that were checked.
    The group name, why and plan are left out; any change to a leg changes it."""
    blob = json.dumps(group.get("legs"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def parse_sec_list(text):
    """NSE sec_list.csv -> {SYMBOL: {"series", "band" (percent, None for "No Band"), "name", "remarks"}}.
    A symbol listed in more than one series keeps its EQ row."""
    out = {}
    rows = csv.reader(io.StringIO(text.lstrip("﻿")))
    head = [h.strip().lower() for h in next(rows, [])]
    try:
        si, ei = head.index("symbol"), head.index("series")
    except ValueError:
        raise RiskError("sec_list.csv has no Symbol / Series columns")
    bi = head.index("band") if "band" in head else None
    ni = head.index("security name") if "security name" in head else None
    ri = head.index("remarks") if "remarks" in head else None
    for r in rows:
        if len(r) <= max(si, ei):
            continue
        sym, series = r[si].strip().upper(), r[ei].strip().upper()
        if not sym or (sym in out and out[sym]["series"] == "EQ"):
            continue
        band = _f(r[bi]) if bi is not None and bi < len(r) else None
        out[sym] = {"series": series, "band": band,
                    "name": r[ni].strip() if ni is not None and ni < len(r) else "",
                    "remarks": r[ri].strip() if ri is not None and ri < len(r) else ""}
    return out


def load_bands(path):
    """nse/state/bands.json (or a sec_list.csv) -> (bands, as_of) or (None, None) if there is none.
    as_of is None when the file's as_of is missing or not YYYY-MM-DD: check() treats that as stale."""
    if not path or not os.path.exists(path):
        return None, None
    if path.lower().endswith(".csv"):
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path), datetime.timezone.utc)
        return parse_sec_list(text), to_ist(mtime).date().isoformat()
    doc = read_json(path, "price bands")
    if not isinstance(doc, dict) or not isinstance(doc.get("bands"), dict):
        raise RiskError("%s has no \"bands\" object: run check.py --refresh-bands" % path)
    return doc["bands"], valid_day(doc.get("as_of"))


def valid_day(text):
    """"YYYY-MM-DD" if text is a real ISO date, else None (a missing or malformed as_of counts as stale)."""
    if not isinstance(text, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", text.strip()):
        return None
    try:
        return datetime.date.fromisoformat(text.strip()).isoformat()
    except ValueError:
        return None


def fetch_sec_list(offline=None):
    """(text, source) of sec_list.csv: from the --offline folder or file, else the live NSE archive."""
    if offline:
        src = os.path.join(offline, "sec_list.csv") if os.path.isdir(offline) else offline
        try:
            with open(src, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            raise RiskError("no sec_list.csv at %s: %s" % (src, e))
    else:
        src = SEC_LIST_URL
        req = urllib.request.Request(SEC_LIST_URL, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8", "replace")
        except Exception as e:
            raise RiskError("could not fetch %s: %s" % (SEC_LIST_URL, e))
    return text, src


def refresh_bands(path, today, offline=None):
    """Fetch sec_list.csv (or read it from the --offline folder) and write the bands cache.
    A .csv --bands path is not overwritten: the fresh bands are used in memory only."""
    text, src = fetch_sec_list(offline)
    bands = parse_sec_list(text)
    if not bands:
        raise RiskError("sec_list.csv from %s has no rows" % src)
    if path.lower().endswith(".csv"):
        return bands
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {"as_of": today, "source": src, "fetched": datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ"), "bands": bands}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return bands


def load_universe(path):
    doc = read_json(path, "universe", required=False)
    if doc is None:
        return None
    syms = doc.get("symbols", []) if isinstance(doc, dict) else doc
    return {str(s.get("symbol") if isinstance(s, dict) else s).strip().upper() for s in syms or []}


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _d(x):
    """A price or amount -> Decimal to the paisa, or None."""
    v = _f(x)
    return None if v is None else Decimal(repr(v)).quantize(PAISA)


def _fl(x):
    return None if x is None else float(Decimal(x).quantize(PAISA))


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def account(cfg, portfolio, prices):
    """Starting account: cash, shares and position values (Decimal). No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _d(cfg.get("budget"))
        if not budget:
            raise RiskError("no nse/portfolio.json and no budget in the config: nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _d(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = str(p.get("symbol") or p.get("tradingsymbol") or p.get("ticker") or "").strip().upper()
        if not t:
            continue
        n = _f(p.get("qty", p.get("quantity", p.get("shares"))))
        if n is None:
            n = sum(_f(l.get("shares", l.get("qty"))) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + int(round(n))
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = _d(sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))) or Decimal("0.00")
            values[t] = basis
            warnings.append("no price for %s: valued at its cost basis %s" % (t, inr(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def broker_records(state):
    """nse/state/broker.json -> a list of records ("orders" as a list, or keyed by group fingerprint)."""
    orders = (state or {}).get("orders", [])
    if isinstance(orders, dict):
        out = []
        for fp, rec in orders.items():
            if isinstance(rec, dict):
                rec = dict(rec)
                rec.setdefault("fingerprint", fp)
                out.append(rec)
        return out
    return [r for r in orders if isinstance(r, dict)]


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_symbol(rec):
    return str(rec.get("symbol") or rec.get("tradingsymbol") or "").strip().upper()


def rec_side(rec):
    return str(rec.get("side") or rec.get("transaction_type") or "").strip().upper()


def rec_is_entry(rec):
    """nse-broker logs the kind nse-risk gave; a record without one: a BUY is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return rec_side(rec) == "BUY"


def rec_orders(rec):
    """How many Kite orders a record stands for: its legs, else one."""
    legs = rec.get("legs")
    if isinstance(legs, list):
        return max(len(legs), 1)
    n = _f(legs)
    return int(n) if n and n > 0 else 1


def placed_today(state, today):
    return [r for r in broker_records(state) if r.get("status") == "placed" and rec_day(r) == today]


# ---------------------------------------------------------------- one group's legs

def _article(t):
    return "an" if t in ("SL", "SL-M") else "a"


def read_legs(o):
    """(legs, format reasons, warnings, symbol, exchange). Every leg gets a "role":
    entry, stop, target or exit (inferred when the leg has none)."""
    reasons, warns = [], []
    raw = o.get("legs")
    if not isinstance(raw, list) or not raw:
        return [], ["format: no legs (a group needs an entry, a stop and a target leg from nse-budget)"], warns, \
            "", ""
    legs = []
    for n, lg in enumerate(raw, 1):
        if not isinstance(lg, dict):
            reasons.append("format: leg %d is not an object" % n)
            continue
        name = str(lg.get("role") or "leg %d" % n)
        side = str(lg.get("transaction_type") or "").strip().upper()
        typ = str(lg.get("order_type") or "").strip().upper()
        if not str(lg.get("tradingsymbol") or "").strip():
            reasons.append("format: the %s leg has no tradingsymbol" % name)
        if side not in ("BUY", "SELL"):
            reasons.append("format: the %s leg's transaction_type must be BUY or SELL, got %r"
                           % (name, lg.get("transaction_type")))
        q = lg.get("quantity")
        qf = _f(q) if not isinstance(q, bool) else None
        if qf is None or qf != int(qf):
            reasons.append("format: quantity must be a whole number of shares, got %r (the %s leg)" % (q, name))
            qty = None
        elif qf <= 0:
            reasons.append("format: quantity must be positive, got %r (the %s leg)" % (q, name))
            qty = None
        else:
            qty = int(qf)
        if typ not in ORDER_TYPES:
            reasons.append("format: unknown order_type %r on the %s leg (Kite takes %s)"
                           % (lg.get("order_type"), name, ", ".join(ORDER_TYPES)))
        for k in ("price", "trigger_price"):
            if lg.get(k) is not None and (_f(lg.get(k)) is None or _f(lg.get(k)) <= 0):
                reasons.append("format: the %s leg's %s must be a positive number, got %r" % (name, k, lg.get(k)))
        if typ in ("LIMIT", "SL") and _f(lg.get("price")) is None:
            reasons.append("format: %s %s leg needs a price (the %s leg)" % (_article(typ), typ, name))
        if typ in ("SL", "SL-M") and _f(lg.get("trigger_price")) is None:
            reasons.append("format: %s %s leg needs a trigger_price (the %s leg)" % (_article(typ), typ, name))
        variety = str(lg.get("variety") or "regular").strip().lower()
        if variety not in VARIETIES:
            reasons.append("format: unknown variety %r on the %s leg" % (lg.get("variety"), name))
        validity = str(lg.get("validity") or "DAY").strip().upper()
        if validity not in VALIDITIES:
            reasons.append("format: unknown validity %r on the %s leg" % (lg.get("validity"), name))
        legs.append({"role": str(lg.get("role") or "").strip().lower(), "name": name,
                     "symbol": str(lg.get("tradingsymbol") or "").strip().upper(),
                     "exchange": str(lg.get("exchange") or "").strip().upper(), "side": side, "type": typ,
                     "qty": qty, "price": _d(lg.get("price")), "trigger": _d(lg.get("trigger_price")),
                     "product": str(lg.get("product") or "").strip().upper()})
    syms = sorted({lg["symbol"] for lg in legs if lg["symbol"]})
    if len(syms) > 1:
        reasons.append("format: every leg must be the same symbol (%s)" % ", ".join(syms))
    exchs = sorted({lg["exchange"] for lg in legs if lg["exchange"]})
    if len(exchs) > 1:
        reasons.append("format: every leg must be on the same exchange (%s)" % ", ".join(exchs))
    if any(not lg["exchange"] for lg in legs):
        reasons.append("format: every leg needs an exchange (NSE or BSE)")
    # roles: entry, stop, target, exit
    has_entry = any(lg["role"] == "entry" for lg in legs)
    for lg in legs:
        if lg["role"] in ("entry", "stop", "target", "exit"):
            continue
        if not has_entry and lg["side"] == "BUY" and not any(x["side"] == "SELL" and x["role"] == "entry"
                                                             for x in legs):
            lg["role"], has_entry = "entry", True
        elif lg["type"] in ("SL", "SL-M"):
            lg["role"] = "stop"
        elif lg["type"] == "LIMIT" and has_entry:
            lg["role"] = "target"
        else:
            lg["role"] = "exit"
    return legs, reasons, warns, (syms[0] if syms else ""), (exchs[0] if exchs else "")


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None,
          bands=None, prev_close=None, universe=None, bands_as_of=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    orders       order groups as nse-budget writes them ({"group", "legs", "why", "plan"})
    now          aware UTC (naive is taken as UTC; default: the real clock); its IST date is "today"
    price_times  {SYM: UTC time of that price}; None = every price counts as fresh
    bands        {SYM: {"series", "band"}} from sec_list.csv (parse_sec_list); None = not loaded
    prev_close   {SYM: previous close} from today (load_prices drops an older one)
    bands_as_of  IST date (YYYY-MM-DD) of the bands; missing, malformed or before today blocks entries
    universe     EQ symbols from nse/universe.json; None = not loaded"""
    live_acct = broker_mode(cfg) == "kite"
    c = dict(DEFAULTS)
    c.update(cfg or {})
    prices = {str(k).upper(): _d(v) for k, v in (prices or {}).items() if _f(v)}
    prev_close = {str(k).upper(): _d(v) for k, v in (prev_close or {}).items() if _f(v)}
    universe = None if universe is None else {str(s).strip().upper() for s in universe}
    now_utc = _aware(now) or parse_now(None)
    local = to_ist(now_utc)
    today = today or local.date().isoformat()
    chg_cfg = c.get("charges") or {}
    acct, top_warn = account(c, portfolio, prices)
    blocked = {str(b).strip().upper() for b in c.get("blocked") or []}
    done_today = placed_today(state, today)
    orders_today = sum(rec_orders(r) for r in done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = Decimal("0.00")
    for r in entries_today:
        ml = _d(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (rec_side(r), rec_symbol(r)))
        else:
            loss_today += ml
    placed_keys = {(rec_symbol(r), rec_side(r)) for r in done_today}
    win_reasons, win_warns = window_check(now_utc, c)
    cal = calendar_warning(local.date(), c)
    if cal:
        top_warn.append(cal)
    if live_acct:
        top_warn.insert(0, "LIVE account (\"broker\": \"kite\"): these orders use real money")
    top_warn.extend(win_warns)
    seen = set()
    results = []
    batch_entries, batch_loss, batch_orders = 0, Decimal("0.00"), 0

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = _aware(price_times.get(sym))
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        o = o if isinstance(o, dict) else {}
        group = str(o.get("group") or "#%d" % i)
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        legs, fmt, lw, sym, exch = read_legs(o)
        reasons.extend(fmt)
        warns.extend(lw)
        entry_leg = next((lg for lg in legs if lg["role"] == "entry"), None)
        stop_legs = [lg for lg in legs if lg["role"] == "stop"]
        target_legs = [lg for lg in legs if lg["role"] == "target"]
        exit_legs = [lg for lg in legs if lg["role"] == "exit"]

        # entry or exit
        held = acct["shares"].get(sym, 0)
        kind, side, qty = "entry", "BUY", None
        if entry_leg is not None:
            side, qty = entry_leg["side"], entry_leg["qty"]
            if qty is not None and side == "SELL" and held > 0 and qty <= held:
                kind = "exit"
            elif qty is not None and side == "BUY" and held < 0 and qty <= -held:
                kind = "exit"
        elif legs:
            sides = {lg["side"] for lg in legs}
            side = "SELL" if sides == {"SELL"} else ("BUY" if sides == {"BUY"} else "")

            def total(ls):
                return sum(lg["qty"] or 0 for lg in ls)
            qty = total(exit_legs) + max(total(stop_legs), total(target_legs))
            cover = held if side == "SELL" else -held
            if side and qty and cover > 0 and qty <= cover:
                kind = "exit"
            elif not side:
                reasons.append("format: a group with BUY and SELL legs needs an entry leg (\"role\": \"entry\")")
            else:
                entry_leg = (exit_legs or legs)[0]
        long = side != "SELL"

        # fno and the exchange
        if exch in FNO_EXCHANGES:
            if not c.get("allow_fno"):
                reasons.append("fno: %s is futures and options; F&O is off (\"allow_fno\": false)" % exch)
            else:
                warns.append("F&O order on %s: futures and options can lose more than you put in" % exch)
        elif exch and exch not in EQUITY_EXCHANGES:
            later.append("legs: exchange %s: this pack trades NSE and BSE equity" % exch)

        # series
        if sym:
            b = bands.get(sym) if bands is not None else None
            if b is not None:
                series = str(b.get("series") or "").upper()
                if series != "EQ":
                    reasons.append("series: %s trades in the %s series, not EQ: trade-to-trade or SME, so no "
                                   "intraday (MIS) trading" % (sym, series or "unknown"))
            elif exch in FNO_EXCHANGES or (universe is not None and sym in universe):
                pass
            elif universe is not None or bands is not None:
                later.append("series: %s is not in the EQ universe; refresh bands or universe (check.py "
                             "--refresh-bands, nse-scanner/scan.py universe)" % sym)
            else:
                warns.append("series unknown: %s: neither the price-band list nor nse/universe.json is loaded; "
                             "check it is an EQ stock before you trade it" % sym)

        # the reference price: what one share costs to get in
        plan_entry = _d(plan.get("entry"))
        live = fresh(sym)
        ref, ref_from = None, None
        if kind == "entry" and entry_leg is not None:
            if entry_leg["type"] in ("LIMIT", "SL") and entry_leg["price"]:
                ref, ref_from = entry_leg["price"], "limit"
            elif entry_leg["type"] == "SL-M" and entry_leg["trigger"]:
                ref, ref_from = entry_leg["trigger"], "trigger"
            if ref is None:
                cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
                if cands:
                    ref, ref_from = max(cands, key=lambda x: x[0])
                    if ref_from == "price" and plan_entry:
                        info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                    % (sym, inr(ref), inr(plan_entry), inr(ref)))
                elif sym in prices:
                    ref, ref_from = prices[sym], "stale price"
            if (plan_entry and live is None and sym in prices and price_times is not None
                    and entry_leg["type"] == "MARKET"):
                warns.append("the %s price in nse/prices.json is older than %d minutes: not used"
                             % (sym, FRESH_MINUTES))
        elif kind == "exit":
            ref = prices.get(sym) or next((lg["price"] for lg in legs if lg["price"]), None)
        value = qty * ref if (qty and ref) else None
        if kind == "entry" and value is None and qty:
            later.append("price: no price for %s to size a %d-share order; add it to nse/prices.json "
                         "(nse-alerts: alerts.py quote %s --write)" % (sym, qty, sym))

        # tick sizes (Kite rejects a price off the tick)
        for lg in legs:
            for k, v in (("price", lg["price"]), ("trigger", lg["trigger"])):
                if v is not None:
                    tick = tick_for(ref or v)
                    if v % tick != 0:
                        warns.append("tick: the %s leg's %s %s is not a multiple of the %s tick; Kite rejects it"
                                     % (lg["name"], k, inr(v), inr(tick)))

        # the legs of a day trade
        stop_leg = stop_legs[0] if stop_legs else None
        target_leg = target_legs[0] if target_legs else None
        if kind == "entry" and legs:
            qtys = [lg["qty"] for lg in legs]
            if len(set(qtys)) > 1:
                later.append("legs: the legs' quantities differ (%s); the stop and target must cover the entry "
                             "exactly" % ", ".join("?" if q is None else str(q) for q in qtys))
            if stop_leg and stop_leg["type"] not in ("SL-M", "SL"):
                later.append("legs: the stop leg must be SL-M or SL, not %s" % (stop_leg["type"] or "blank"))
            exit_side = "SELL" if long else "BUY"
            for lg in stop_legs + target_legs:
                if lg["side"] and lg["side"] != exit_side:
                    later.append("legs: the %s leg must be a %s for a %s entry" % (lg["name"], exit_side, side))
        for lg in legs:
            if lg["product"] != "MIS":
                later.append("legs: product %s on the %s leg: a day trade is MIS (intraday), closed by your "
                             "flat-by time" % (lg["product"] or "blank", lg["name"]))

        # blocked list and size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        if value is not None:
            mn, mx = _d(c.get("min_order_inr")), _d(c.get("max_order_inr"))
            if mn and value < mn:
                later.append("size: %s is under your %s minimum order" % (inr(value), inr(mn)))
            if mx and value > mx:
                later.append("size: %s is over your %s max order" % (inr(value), inr(mx)))

        # account effects, simulated in order
        cash = acct["cash"]
        total_acct = cash + sum(acct["values"].values(), Decimal("0.00"))
        buffer_inr = (total_acct * Decimal(str(c.get("cash_buffer_pct") or 0)) / 100).quantize(PAISA)
        if value is not None and long and kind == "entry":
            after_cash = cash - value
            if after_cash < 0:
                if not c.get("allow_leverage"):
                    later.append("leverage: needs %s but only %s cash; that would be broker leverage (MIS margin), "
                                 "which is off (\"allow_leverage\": false)" % (inr(value), inr(cash)))
                else:
                    warns.append("leverage: needs %s with %s cash; the rest is broker MIS margin, and a loss can be "
                                 "bigger than your cash" % (inr(value), inr(cash)))
            elif after_cash < buffer_inr:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (inr(after_cash), _num(c["cash_buffer_pct"]), inr(buffer_inr)))
            held_value = acct["values"].get(sym, Decimal("0.00"))
            pos_after = held_value + value
            cap = total_acct * Decimal(str(c.get("max_position_pct") or 100)) / 100
            if total_acct > 0 and pos_after > cap:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, inr(pos_after), float(100 * pos_after / total_acct), inr(total_acct),
                                _num(c["max_position_pct"])))
        if kind == "entry" and side == "SELL" and sym and not c.get("allow_short"):
            reasons.append("short: selling more %s than you hold (%s shares); short selling is off "
                           "(\"allow_short\": false). If you bought it, record the fill first "
                           "(nse-broker: ticket.py fill)" % (sym, _num(held)))

        # day-trade checks: circuit, stop, loss, reward:risk, daily loss, trades, window
        max_loss = risk_ps = rr = stop = target = ch = None
        if kind == "entry" and side in ("BUY", "SELL"):
            # circuit
            b = bands.get(sym) if (bands is not None and sym) else None
            if exch in FNO_EXCHANGES:
                pass
            elif bands is None:
                later.append("circuit: NSE price bands are not loaded, so the upper-circuit check cannot run: "
                             "run check.py --refresh-bands")
            elif valid_day(bands_as_of) is None:
                later.append("circuit: the NSE price bands have no valid as_of date (got %r), so they count as "
                             "stale: run check.py --refresh-bands" % (bands_as_of,))
            elif valid_day(bands_as_of) < today:
                later.append("circuit: the NSE price bands are from %s, before today (%s): run check.py "
                             "--refresh-bands" % (bands_as_of, today))
            elif b is None:
                warns.append("circuit: no price band for %s (refresh nse/state/bands.json: check.py "
                             "--refresh-bands): the circuit check was skipped" % sym)
            elif _f(b.get("band")) is None:
                info.append("%s has no fixed price band (an F&O stock: NSE sets dynamic bands)" % sym)
            else:
                band = Decimal(str(_f(b.get("band"))))
                if float(band) in TIGHT_BANDS and not c.get("allow_banded"):
                    later.append("circuit: %s has a %s%% price band: it can lock at the limit and you could not get "
                                 "out. Off unless \"allow_banded\": true" % (sym, _num(band)))
                pc = prev_close.get(sym)
                if pc is None:
                    later.append("circuit: no previous close from today for %s in nse/prices.json "
                                 "(detail.prev_close), so the upper band is unknown: run nse-alerts/alerts.py "
                                 "quote %s --write" % (sym, sym))
                elif ref:
                    room = CIRCUIT_TICKS * tick_for(ref)
                    if long:
                        upper = (pc * (100 + band) / 100).quantize(PAISA)
                        if ref >= upper - room:
                            later.append("circuit: the entry %s is within %d ticks of %s's upper band %s (previous "
                                         "close %s + %s%%): a stock locked at its upper circuit cannot be sold"
                                         % (inr(ref), CIRCUIT_TICKS, sym, inr(upper), inr(pc), _num(band)))
                    else:
                        lower = (pc * (100 - band) / 100).quantize(PAISA)
                        if ref <= lower + room:
                            later.append("circuit: the entry %s is within %d ticks of %s's lower band %s (previous "
                                         "close %s - %s%%)" % (inr(ref), CIRCUIT_TICKS, sym, inr(lower), inr(pc),
                                                              _num(band)))
            # stop and target
            plan_stop, plan_target = _d(plan.get("stop")), _d(plan.get("target"))
            stop = stop_leg["trigger"] if stop_leg else None
            target = target_leg["price"] if target_leg else None
            stop_word = "stop trigger"
            if stop and plan_stop and stop != plan_stop:
                warns.append("the stop leg %s differs from the plan's stop %s: the leg is the one the broker uses"
                             % (inr(stop), inr(plan_stop)))
            if not stop_leg:
                if c.get("require_stop", True):
                    later.append("legs: no stop leg. Every entry needs one (\"require_stop\": true): an SL-M %s leg, "
                                 "from nse-exit and nse-budget" % ("SELL" if long else "BUY"))
                elif plan_stop:
                    stop, stop_word = plan_stop, "stop"
                    warns.append("the stop %s is in the plan only, not at the broker: set a stop alert "
                                 "(nse-alerts: alerts.py add --from-plan)" % inr(stop))
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            if target is None and plan_target:
                target = plan_target
            if stop and ref and qty:
                if (long and stop >= ref) or (not long and stop <= ref):
                    later.append("legs: the %s %s must be %s the entry %s"
                                 % (stop_word, inr(stop), "below" if long else "above", inr(ref)))
                else:
                    risk_ps = abs(ref - stop)
                    if long:
                        ch = round_trip(qty, ref, stop, chg_cfg)
                    else:
                        ch = charges(qty * stop, qty * ref, chg_cfg)["total"]
                    max_loss = qty * risk_ps + ch
                    cap_loss = _d(c.get("max_loss_per_trade_inr"))
                    if cap_loss and max_loss > cap_loss:
                        later.append("loss: %d shares x %s risk a share = %s plus %s charges = %s at the stop, over "
                                     "your %s max loss per trade" % (qty, inr(risk_ps), inr(qty * risk_ps), inr(ch),
                                                                     inr(max_loss), inr(cap_loss)))
                    pml = _d(plan.get("max_loss"))
                    if pml is not None and max_loss > pml:
                        info.append("the plan said %s at the stop; measured from %s it is %s"
                                    % (inr(pml), inr(ref), inr(max_loss)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref) or (not long and target >= ref):
                            later.append("legs: the target %s must be %s the entry %s"
                                         % (inr(target), "above" if long else "below", inr(ref)))
                        else:
                            rr = round(float(abs(target - ref) / risk_ps), 2)
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your minimum %s"
                                             % (rr, inr(target), inr(ref), inr(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            daily = _d(c.get("max_daily_loss_inr"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (inr(used), inr(max_loss), inr(used + max_loss), inr(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (inr(ref), REF_WORDS.get(ref_from, ref_from), inr(stop),
                               inr(target) if target else "none", inr(risk_ps)))
                info.append("max loss %s with %s charges%s, reward:risk %s%s"
                            % (inr(max_loss), inr(ch),
                               (" (limit %s)" % inr(c["max_loss_per_trade_inr"]))
                               if _f(c.get("max_loss_per_trade_inr")) else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               inr(loss_today + batch_loss), inr(daily) if daily else "no limit on"))
        later.extend(win_reasons)

        # duplicates and the daily order limit (Kite orders: one per leg)
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side, sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side, sym))
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and orders_today + batch_orders + len(legs) > cap_n:
            later.append("daily limit: %d orders placed today plus %d passed earlier in this batch, this group is "
                         "%d more, your limit is %d a day" % (orders_today, batch_orders, len(legs), cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            batch_orders += len(legs)
            if kind == "entry":
                batch_entries += 1
                batch_loss += max_loss or Decimal("0.00")
            n = qty or 0
            if side == "BUY":
                if value is not None:
                    acct["cash"] -= value
                    acct["values"][sym] = acct["values"].get(sym, Decimal("0.00")) + value
                acct["shares"][sym] = held + n
            elif side == "SELL":
                if value is not None:
                    acct["cash"] += value
                    acct["values"][sym] = max(acct["values"].get(sym, Decimal("0.00")) - value, Decimal("0.00"))
                acct["shares"][sym] = held - n
        results.append({
            "index": i, "group": group, "symbol": sym, "exchange": exch, "side": side, "kind": kind,
            "qty": qty, "legs": len(legs), "value_inr": _fl(value),
            "entry_ref": _fl(ref), "entry_ref_from": ref_from, "stop": _fl(stop), "target": _fl(target),
            "risk_per_share": _fl(risk_ps), "charges": _fl(ch), "max_loss": _fl(max_loss), "reward_risk": rr,
            "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_ist": local.strftime("%Y-%m-%d %H:%M"),
        "paper": not live_acct,
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": _fl(acct["cash"]),
        "today": {"orders_placed": orders_today, "entries_placed": len(entries_today),
                  "loss_at_risk": _fl(loss_today)},
    }


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s: %s %s %s, %s" % (r["verdict"], r["index"], r["group"], r["side"] or "?",
                                                    r["qty"] if r["qty"] is not None else "?", r["symbol"] or "?",
                                                    r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s IST. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], res["now_ist"], inr(res["cash_after"]),
                    "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked groups do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every group passed. nse-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc, today=None):
    """prices.json -> ({SYM: price}, {SYM: price time, aware UTC, or None}, {SYM: previous close}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of. A previous close is kept only when its quote time
    (detail.time, else as_of) is on `today` (IST), so yesterday's file cannot set today's band."""
    if not isinstance(pdoc, dict):
        return {}, {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}, {}
    out, times, prev = {}, {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, d in (detail.items() if isinstance(detail, dict) else []):
        d = d if isinstance(d, dict) else {}
        when = parse_utc(d.get("time")) or as_of
        if _f(d.get("prev_close")) and (today is None or (when and to_ist(when).date().isoformat() == today)):
            prev[str(s).upper()] = _f(d["prev_close"])
    for s, p in prices.items():
        s = str(s).upper()
        d = detail.get(s) or {}
        if _f(p) is None:
            continue
        out[s] = _f(p)
        times[s] = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of
    return out, times, prev


def has_entry(orders):
    """True when any group may be an entry (a BUY or entry-role leg): only those need fresh bands."""
    for o in orders:
        for lg in (o.get("legs") if isinstance(o, dict) and isinstance(o.get("legs"), list) else []):
            if isinstance(lg, dict) and (str(lg.get("role", "")).lower() == "entry"
                                         or str(lg.get("transaction_type", "")).upper() == "BUY"):
                return True
    return False


def write_verdicts(path, res, orders_file):
    """nse/state/risk.json: this check's results plus every verdict for today keyed by fingerprint."""
    old = read_json(path, "risk state", required=False)
    verdicts = {}
    if isinstance(old, dict) and isinstance(old.get("verdicts"), dict):
        verdicts = {k: v for k, v in old["verdicts"].items()
                    if isinstance(v, dict) and v.get("date") == res["date"]}
    checked_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    for r in res["results"]:
        verdicts[r["fingerprint"]] = {"group": r["group"], "date": res["date"], "verdict": r["verdict"],
                                      "kind": r["kind"], "symbol": r["symbol"], "side": r["side"], "qty": r["qty"],
                                      "max_loss": r["max_loss"], "reasons": r["reasons"], "checked_at": checked_at,
                                      "now_ist": res["now_ist"]}
    rec = dict(res)
    rec["checked_at"] = checked_at
    rec["orders_file"] = orders_file
    rec["verdicts"] = verdicts
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed order groups against your limits (nse-risk).")
    ap.add_argument("--orders", default="nse/orders.json")
    ap.add_argument("--config", default="nse/config.json")
    ap.add_argument("--portfolio", default="nse/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="nse/prices.json")
    ap.add_argument("--state", default="nse/state/broker.json", help="what nse-broker placed today")
    ap.add_argument("--bands", default="nse/state/bands.json",
                    help="price bands: the cache --refresh-bands writes, or a sec_list.csv")
    ap.add_argument("--universe", default="nse/universe.json", help="EQ symbols from nse-scanner")
    ap.add_argument("--refresh-bands", action="store_true",
                    help="fetch today's sec_list.csv from nsearchives.nseindia.com into --bands first")
    ap.add_argument("--offline", metavar="DIR", help="read DIR/sec_list.csv for --refresh-bands; never fetch the bands live")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-27 10:15\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")
    ap.add_argument("--out", default="nse/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        try:
            now = parse_now(a.now)
        except ValueError as e:
            raise RiskError(str(e))
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-10-27")
        today = a.today or to_ist(now).date().isoformat()
        doc = read_json(a.orders, "orders file (run nse-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times, prev = load_prices(pdoc, today)
        state = read_json(a.state, "broker state", required=False)
        refresh_note = None
        if a.refresh_bands:
            bands, bands_as_of = refresh_bands(a.bands, today, a.offline), today
        else:
            bands, bands_as_of = load_bands(a.bands)
            if ((bands is None or bands_as_of is None or bands_as_of < today) and not a.offline
                    and has_entry(orders)):
                try:   # one live try; without it the entries are blocked (circuit)
                    bands, bands_as_of = refresh_bands(a.bands, today), today
                    refresh_note = "price bands refreshed from %s" % SEC_LIST_URL
                except RiskError as e:
                    refresh_note = "could not refresh the price bands (%s)" % e
        universe = load_universe(a.universe)
        res = check(orders, cfg, portfolio, prices, state, today, now, times, bands=bands, prev_close=prev,
                    universe=universe, bands_as_of=bands_as_of)
        if refresh_note:
            res["warnings"].append(refresh_note)
        if bands is None:
            res["warnings"].append("price bands not loaded (no %s): run check.py --refresh-bands for the series "
                                   "and circuit checks" % a.bands)
        elif bands_as_of is None:
            res["warnings"].append("price bands in %s have no valid as_of date (missing or not YYYY-MM-DD): treated "
                                   "as stale, run check.py --refresh-bands" % a.bands)
        elif bands_as_of != today:
            res["warnings"].append("price bands are from %s: refresh them daily (check.py --refresh-bands)"
                                   % bands_as_of)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            write_verdicts(a.out, res, a.orders)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, ensure_ascii=False) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
