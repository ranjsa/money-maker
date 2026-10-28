#!/usr/bin/env python3
"""Position sizing for nse-budget. Standard library only, no network.

Sizes ONE intraday (MIS) long trade from nse/plan.json (written by nse-exit)
to your budget and your max loss per trade, then writes one three-leg order
(entry, stop-loss, target) to nse/orders.json using Kite Connect place_order
field names. It never places anything: nse-risk checks the order and
nse-broker asks you yes or no.

    python3 size.py                                  # nse/plan.json + nse/config.json
    python3 size.py --write                          # write nse/orders.json
    python3 size.py --limit                          # a LIMIT entry at the plan's entry
    python3 size.py --limit 1014.50 --write          # a LIMIT entry at 1014.50
    python3 size.py --now "2026-10-27 10:15" --json  # pin the clock (IST)

How many shares. Whole shares only, never fractional.

    qty = the smallest of
      max loss       the largest qty with qty x risk + charges at the stop <= max_loss_per_trade_inr
      position cap   budget x max_position_pct / entry
      max order      max_order_inr / entry
      cash           (budget minus the cash buffer) / entry
                     (and no more than the cash in nse/portfolio.json, if you have one)

Charges are the round trip (buy at the entry, sell at the stop) from the
shared charges block, so the loss you cap is the loss you would really take.
A stop too far away for your max loss is a plain refusal, never a smaller
order. A reward:risk under min_reward_risk is a refusal too. Education and
tooling, not financial advice.
"""

import argparse
import datetime
import json
import os
import sys
from decimal import Decimal, ROUND_FLOOR

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


# Missing config keys fall back to these (the contract's config.example.json).
DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_inr": 80000,
    "min_order_inr": 500,
    "max_loss_per_trade_inr": 1000,
    "max_daily_loss_inr": 3000,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "09:30",
    "no_entries_after": "14:45",
    "flat_by": "15:10",
    "product": "MIS",
    "exchange": "NSE",
    "broker": "paper",
    "paper": True,
    "blocked": [],
    "allow_leverage": False,
    "allow_short": False,
    "allow_fno": False,
    "special_sessions": {},
    "charges": {},
}
FRESH_MINUTES = 15
SETUP_WORDS = {"orb": "opening-range breakout", "level": "level breakout", "flag": "flag", "bounce": "oversold bounce"}
CENT = Decimal("0.01")


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- inputs

def parse_utc(text):
    """"2026-10-27T05:22:13Z" -> aware UTC datetime, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def utc_iso(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc).replace(microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def broker_mode(cfg):
    """"paper" or "kite" from the config as written. LIVE is "broker": "kite"; "paper" must agree."""
    cfg = cfg or {}
    b = str(cfg.get("broker") or "paper").strip().lower()
    if b not in ("paper", "kite"):
        raise SizeError("config \"broker\" must be \"paper\" or \"kite\", not %r" % cfg.get("broker"))
    if cfg.get("paper") is not None and bool(cfg.get("paper")) != (b == "paper"):
        raise SizeError("config error: \"broker\" is \"%s\" but \"paper\" is %s. Set both: \"broker\": \"kite\" "
                        "with \"paper\": false for a live account, or \"broker\": \"paper\" with \"paper\": true"
                        % (b, "true" if cfg.get("paper") else "false"))
    return b


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        if not isinstance(doc, dict):
            raise SizeError("%s is not valid JSON: expected an object" % path)
        broker = broker_mode(doc)
        cfg.update(doc)
        cfg["broker"], cfg["paper"] = broker, broker == "paper"
    return cfg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _d(x):
    try:
        return Decimal(str(x))
    except Exception:
        return None


def price_time(doc, sym):
    """When this symbol's price is from: the quote's own time (detail[sym].time), else when it
    was fetched (detail[sym].fetched), else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("time", "fetched"):
        t = parse_utc(det.get(key))
        if t:
            return t
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, time, fresh) for sym from a prices.json doc, or (None, None, False)."""
    if not isinstance(doc, dict):
        return None, None, False
    p = _f((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- money helpers

def shares_for(amount, per_share):
    """Whole shares that fit in amount at per_share, in exact paise."""
    a, p = int(round(float(amount) * 100)), int(round(float(per_share) * 100))
    if p <= 0 or a <= 0:
        return 0
    return a // p


def _px(x):
    """A price for display: 1005.2 -> "1005.20"."""
    return "%.2f" % float(_d(x).quantize(CENT))


def _num(x):
    x = round(float(x), 2)
    if abs(x - round(x)) < 0.005:
        return str(int(round(x)))
    return "%.2f" % x


def _f2(x):
    """Decimal -> float, 2dp, for JSON."""
    return float(_d(x).quantize(CENT))


# ---------------------------------------------------------------- sizing

def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit  the limit price for a LIMIT entry (None = MARKET)
    price  a FRESH latest price; a market entry is sized at the higher of it and the plan's entry
    cash   the cash in nse/portfolio.json, if there is one
    today  IST date (YYYY-MM-DD); a plan for another day is refused"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = str(plan.get("symbol") or "").strip().upper()
    if not sym:
        raise SizeError("the plan has no symbol (run nse-exit)")
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) intraday trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in IST: make a new plan with nse-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = _d(plan.get("entry")), _d(plan.get("stop")), _d(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run nse-exit); the order needs entry, "
                            "stop and target" % name)
    if sym in {str(b).upper() for b in (c.get("blocked") or [])}:
        raise SizeError("%s is on your blocked list" % sym)

    budget = Decimal(str(budget if budget is not None else (c.get("budget") or 0)))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in nse/config.json")
    buf_pct = Decimal(str(c.get("cash_buffer_pct") or 0))
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")

    if limit is not None:
        ref, ref_from = round_tick(limit, limit), "limit"
    elif price is not None and round_tick(price, price) > entry_plan:
        ref, ref_from = round_tick(price, price), "price"
    else:
        ref, ref_from = entry_plan.quantize(CENT), "plan"
    stop, target = stop.quantize(CENT), target.quantize(CENT)
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_px(stop), _px(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with nse-exit" % (_px(target), _px(ref)))
    risk = ref - stop
    reward = target - ref
    chg = c.get("charges") or {}

    cash_buffer = (budget * buf_pct / 100).quantize(CENT)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (inr(budget), inr(cash_buffer))
    if cash is not None and Decimal(str(cash)) - cash_buffer < available - Decimal("0.005"):
        available = Decimal(str(cash)) - cash_buffer
        cash_note = "cash %s in nse/portfolio.json minus the %s buffer" % (inr(cash), inr(cash_buffer))
    available = max(available.quantize(CENT), Decimal("0"))

    def loss_at_stop(q):
        return q * risk + round_trip(q, ref, stop, chg)

    limits = []
    max_loss_cfg = _d(c.get("max_loss_per_trade_inr"))
    if max_loss_cfg:
        q = int((max_loss_cfg / risk).to_integral_value(rounding=ROUND_FLOOR))
        while q >= 1 and loss_at_stop(q) > max_loss_cfg:
            q -= 1
        limits.append({"name": "max loss", "amount": _f2(max_loss_cfg), "per": _f2(risk), "shares": max(q, 0),
                       "how": "%s max loss, %s risk a share plus charges at the stop" % (inr(max_loss_cfg), inr(risk))})
    cap_pct = _d(c.get("max_position_pct"))
    if cap_pct:
        cap = (budget * cap_pct / 100).quantize(CENT)
        limits.append({"name": "position cap", "amount": _f2(cap), "per": _f2(ref), "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), inr(budget), inr(cap), inr(ref))})
    max_order = _d(c.get("max_order_inr"))
    if max_order:
        limits.append({"name": "max order", "amount": _f2(max_order), "per": _f2(ref),
                       "shares": shares_for(max_order, ref), "how": "%s / %s" % (inr(max_order), inr(ref))})
    limits.append({"name": "cash", "amount": _f2(available), "per": _f2(ref), "shares": shares_for(available, ref),
                   "how": "%s = %s / %s" % (cash_note, inr(available), inr(ref))})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    cost = qty * ref
    ch = round_trip(qty, ref, stop, chg) if qty else Decimal("0.00")
    loss = qty * risk + ch
    need = risk + round_trip(1, ref, stop, chg)
    refusal = None
    if qty == 0:
        if bound[0] == "max loss":
            refusal = ("a stop %s away needs max loss ≥ %s (one share risks %s plus %s round-trip charges); "
                       "your max loss is %s. No order, never a smaller one. Skip this trade, or plan a closer "
                       "stop with nse-exit." % (inr(risk), inr(need), inr(risk), inr(round_trip(1, ref, stop, chg)),
                                                inr(max_loss_cfg)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, inr(ref), bound[0], limits[[l["name"] for l in limits].index(bound[0])]["how"]))
    elif _f(c.get("min_reward_risk")) and reward < Decimal(str(c["min_reward_risk"])) * risk:
        refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %s, so nse-risk "
                   "would block it. No order. Skip it, or make a new plan with nse-exit."
                   % (reward / risk, inr(target), inr(ref), inr(stop), _num(c["min_reward_risk"])))
    elif cost < Decimal(str(c.get("min_order_inr") or 0)):
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, inr(cost), inr(c["min_order_inr"])))
    return {
        "symbol": sym, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": _f2(entry_plan),
        "entry": _f2(ref), "entry_from": ref_from, "stop": _f2(stop), "target": _f2(target),
        "risk_per_share": _f2(risk), "reward_risk": round(float(reward / risk), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": _f2(budget), "cash_buffer_pct": float(buf_pct), "cash_buffer": _f2(cash_buffer),
        "cash_available": _f2(available), "max_loss_per_trade_inr": _f2(max_loss_cfg) if max_loss_cfg else None,
        "limits": limits, "qty": qty, "bound_by": bound[0], "cost": _f2(cost),
        "charges": _f2(ch), "max_loss": _f2(loss), "min_loss_needed": _f2(need),
        "flat_by": plan.get("flat_by") or c.get("flat_by"),
        "exchange": plan.get("exchange") or c.get("exchange") or "NSE",
        "product": plan.get("product") or c.get("product") or "MIS",
        "order_type": "LIMIT" if limit is not None else "MARKET", "refusal": refusal,
    }


def group_number(path, symbol, date):
    """1 + the groups for this symbol and date already in the orders file (if there is one)."""
    prefix = "%s-%s-" % (symbol, date)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        n = sum(1 for o in doc.get("orders", []) if str(o.get("group", "")).startswith(prefix))
    except (OSError, ValueError, AttributeError):
        n = 0
    return n + 1


def kite_order(s, n=1):
    """The one proposed order: three Kite Connect place_order legs plus why and plan."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    base = {"exchange": s["exchange"], "tradingsymbol": s["symbol"], "quantity": int(s["qty"]),
            "product": s["product"], "variety": "regular", "validity": "DAY"}

    def leg(role, side, otype, **extra):
        d = {"role": role, "exchange": base["exchange"], "tradingsymbol": base["tradingsymbol"],
             "transaction_type": side, "quantity": base["quantity"], "product": base["product"],
             "order_type": otype}
        d.update(extra)
        d["variety"], d["validity"] = base["variety"], base["validity"]
        return d

    entry = leg("entry", "BUY", s["order_type"])
    if s["order_type"] == "LIMIT":
        entry["price"] = s["entry"]
        entry = {k: entry[k] for k in ("role", "exchange", "tradingsymbol", "transaction_type", "quantity",
                                       "product", "order_type", "price", "variety", "validity")}
    legs = [entry,
            leg("stop", "SELL", "SL-M", trigger_price=s["stop"]),
            leg("target", "SELL", "LIMIT", price=s["target"])]
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "intraday trade")
    why = ("%s on %s: %d shares, bound by your %s (%d x %s = %s plus %s charges = %s at the stop)"
           % (setup, s["symbol"], s["qty"],
              {"max loss": "%s max loss per trade" % inr(s["max_loss_per_trade_inr"] or 0),
               "position cap": "position cap", "max order": "max order",
               "cash": "cash after the buffer"}[s["bound_by"]],
              s["qty"], inr(s["risk_per_share"]), inr(s["qty"] * s["risk_per_share"]), inr(s["charges"]),
              inr(s["max_loss"])))
    return {
        "group": "%s-%s-%d" % (s["symbol"], s["date"], n),
        "legs": legs,
        "why": why,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "charges": s["charges"], "max_loss": s["max_loss"],
                 "setup": s["setup"], "entry_time": s["entry_time"], "flat_by": s["flat_by"]},
    }


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy, %s, entry %s%s" % (s["symbol"], setup, inr(s["entry_plan"]),
                                            (" at " + s["entry_time"] + " IST") if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (inr(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             inr(s["target"]),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry minus stop). Reward:risk %.2f."
                 % (inr(s["entry"]), src, inr(s["risk_per_share"]), s["reward_risk"]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-70s %5d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lines.append("Size: %d shares of %s = %s at %s." % (s["qty"], s["symbol"], inr(s["cost"]), inr(s["entry"])))
    lines.append("Max loss at the stop: %d x %s = %s, plus %s charges (buy and sell, STT, GST...) = %s."
                 % (s["qty"], inr(s["risk_per_share"]), inr(s["qty"] * s["risk_per_share"]), inr(s["charges"]),
                    inr(s["max_loss"])))
    lines.append("A stop is an order, not a promise: in a fast market it can fill below %s." % inr(s["stop"]))
    lines.append("")
    lines.append("Proposed order %s (nothing has been placed). Kite has no bracket orders for retail, so it is "
                 "three separate legs:" % order["group"])
    for lg in order["legs"]:
        px = ""
        if "trigger_price" in lg:
            px = " trigger %s" % inr(lg["trigger_price"])
        elif "price" in lg:
            px = " at %s" % inr(lg["price"])
        lines.append("  %-6s %s %d %s, %s%s, %s, %s" % (lg["role"], lg["transaction_type"], lg["quantity"],
                                                        lg["tradingsymbol"], lg["order_type"], px, lg["product"],
                                                        lg["validity"]))
    lines.append("When one exit leg fills, the other must be cancelled (nse-broker prints the cancel ticket).")
    if s["flat_by"]:
        lines.append("Flat by %s IST: anything still open then gets closed (nse-alerts rings, nse-broker flatten)."
                     % s["flat_by"])
    lines.append("")
    lines.append("Next: nse-risk checks it, then nse-broker asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "nse-budget",
        "created_at": utc_iso(now),
        "status": "proposed",
        "paper": broker_mode(cfg) == "paper",
        "note": ("Proposals only. Nothing is placed until nse-risk passes each order "
                 "and you say yes to it in the chat."),
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    try:
        now = parse_now(a.now)
    except ValueError as e:
        raise SizeError(str(e))
    today = to_ist(now).date().isoformat()
    plan = read_json(a.plan, "plan (run nse-exit first)")
    limit = None
    if a.limit is not None:
        if a.limit == "plan":
            limit = _d((plan or {}).get("entry"))
        else:
            limit = _d(a.limit)
        if limit is None or limit <= 0:
            raise SizeError("--limit wants a price, like --limit 1014.50")
    sym = str((plan or {}).get("symbol") or "").upper()
    notes, price = [], None
    ppath = a.prices or ("nse/prices.json" if os.path.exists("nse/prices.json") else None)
    if ppath and limit is None and sym:
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now)
        if p is not None and fresh:
            price = p
            if round_tick(p, p) > (_d(plan.get("entry")) or 0):
                notes.append("%s last %s (as of %s IST) is above the plan's entry %s, so the risk "
                             "is bigger: sized at %s." % (sym, inr(p), to_ist(t).strftime("%H:%M"),
                                                          inr(plan.get("entry")), inr(round_tick(p, p))))
        elif p is not None:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + to_ist(t).strftime("%Y-%m-%d %H:%M") + " IST")
                            if t else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today)
    order = None
    if not s["refusal"]:
        n = group_number(a.write, s["symbol"], s["date"] or today) if a.write else 1
        s["date"] = s["date"] or today
        order = kite_order(s, n)
    if order and a.write:
        src = "nse/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes},
                         indent=2, ensure_ascii=False))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed order (3 legs) to %s" % a.write)
    return 0 if order else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size an intraday trade to your budget and max loss (nse-budget).")
    ap.add_argument("--plan", default="nse/plan.json", help="the trade plan (default nse/plan.json)")
    ap.add_argument("--config", default="nse/config.json", help="rules (default nse/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a LIMIT entry instead of MARKET, at the plan's entry or at PRICE")
    ap.add_argument("--prices", help="prices JSON (default nse/prices.json if it exists)")
    ap.add_argument("--portfolio", default="nse/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-27 10:15\"")
    ap.add_argument("--write", nargs="?", const="nse/orders.json", metavar="PATH",
                    help="write the proposed order (default nse/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
