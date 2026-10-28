#!/usr/bin/env python3
"""Order tickets and the decision log for nse-broker. Standard library only.

This tool never talks to a broker and never places anything. It does its
work around the one moment that matters, your yes:

  show      renders each proposed group (entry, stop-loss, target) leg by leg
            in plain words, plus the exact JSON Claude will send to the Kite
            MCP place_order tool for each leg. A group is READY only if
            nse-risk passed THIS exact group (matched by fingerprint) today,
            less than 30 minutes ago on the real clock, and nobody has decided
            on it yet. An older or clock-pinned check says RECHECK.
  approve   records your explicit yes for ONE group, with your reply word for
            word. The reply must start with yes and name the stock and the
            quantity ("yes 73 DEMO"). It refuses "yes to all", "sure", "ok",
            a yes that names a different stock or amount, a yes that could
            mean two groups, and any group that is not READY. With
            "broker": "paper" (the default) it hands the group to paper.py;
            with "broker": "kite" it prints the place_order call for the entry.
  decline   records a no.
  result    records what happened to one leg after Claude's Needs approval
            card: placed (with Kite's order id), denied, or failed.
  fill      records a leg's fill in nse/state/broker.json and nse/portfolio.json.
            After the entry it prints the stop-loss and target place_order
            calls; after the stop or the target it prints the cancel_order
            call for the other leg, so it is never left open.
  flatten   builds a SELL ticket that closes a symbol's position by flat_by.
            It still goes through nse-risk and your yes.
  log       prints today's decisions.

    python3 ticket.py show                       # every group, READY or not
    python3 ticket.py show --index 1 --json      # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "yes 73 DEMO"
    python3 ticket.py result --index 1 --leg entry --status placed --order-id 2510270001
    python3 ticket.py fill --group DEMO-2026-10-27-1 --leg entry --price 1015.00
    python3 ticket.py flatten --symbol DEMO --qty 73
    python3 ticket.py log

Kite has no bracket orders for retail: the entry, the SL-M stop and the LIMIT
target are three separate orders. Place the entry; only after it fills place
the stop and the target; when one of them fills, cancel the other.

Everything is written to nse/state/broker.json (append only). Dates are IST
dates; --now "2026-10-27 10:15" pins the date of the records (the freshness of
the risk check is always judged on the real clock). Not financial advice.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

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

# Kite MCP place_order parameters (zerodha/kite-mcp-server mcp/post_tools.go, checked 2026-10-28).
# The leg keys nse-budget writes are these names already; "role" is ours and is never sent.
KITE_PLACE_ORDER = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
                    "order_type", "price", "validity", "validity_ttl", "disclosed_quantity", "trigger_price",
                    "iceberg_legs", "iceberg_quantity", "tag")
INT_PARAMS = ("quantity", "validity_ttl", "disclosed_quantity", "iceberg_legs", "iceberg_quantity")
FLOAT_PARAMS = ("price", "trigger_price")
BROKERS = ("paper", "kite")
FRESH_MINUTES = 30
ROLES = ("entry", "stop", "target", "exit")
PRODUCT_WORDS = {"MIS": "intraday (MIS)", "CNC": "delivery (CNC)", "NRML": "carry forward (NRML)",
                 "MTF": "margin trading (MTF)"}
VALIDITY_WORDS = {"DAY": "good for today", "IOC": "immediate or cancel"}
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}
NUM_RE = re.compile(r"(?<![A-Za-z&\-])\d[\d,]*(?:\.\d+)?(?![A-Za-z&\-])")


class TicketError(Exception):
    pass


# ---------------------------------------------------------------- time and files

def real_now():
    """The real clock, aware UTC. The risk check's freshness is always judged on it."""
    return _dt.datetime.now(_UTC).replace(microsecond=0)


def ist_iso(now_utc):
    """aware UTC -> "2026-10-27T10:15:00+05:30"."""
    return to_ist(now_utc).replace(microsecond=0).isoformat()


def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def rec_day(rec):
    """The trading day a record belongs to: its "date", else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _num(v):
    f = float(v)
    return str(int(f)) if f == int(f) else repr(f)


def broker_of(cfg):
    b = str((cfg or {}).get("broker", "paper") or "paper").strip().lower()
    if b not in BROKERS:
        raise TicketError("config \"broker\" must be \"paper\" or \"kite\", not %r" % (cfg or {}).get("broker"))
    return b


# ---------------------------------------------------------------- the group

def fingerprint(group):
    """Same function as nse-risk/check.py: sha256 of the group's legs as canonical JSON."""
    blob = json.dumps(group.get("legs"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def role_legs(group):
    """[(role, leg)] in file order. A leg without a role is inferred the way nse-risk does."""
    out, has_entry = [], False
    for lg in group.get("legs") or []:
        if not isinstance(lg, dict):
            continue
        role = str(lg.get("role") or "").strip().lower()
        typ = str(lg.get("order_type") or "").upper()
        side = str(lg.get("transaction_type") or "").upper()
        if role not in ROLES:
            if not has_entry and side == "BUY":
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


def leg(group, role):
    for r, lg in role_legs(group):
        if r == role:
            return lg
    return None


def main_leg(group):
    rl = role_legs(group)
    for r, lg in rl:
        if r == "entry":
            return lg
    return rl[0][1] if rl else {}


def group_symbol(group):
    return str(main_leg(group).get("tradingsymbol") or "").strip().upper()


def group_qty(group):
    q = _f(main_leg(group).get("quantity"))
    return int(q) if q is not None else None


def tool_input(lg):
    """The exact arguments for the Kite MCP place_order tool: Kite's parameter names, the
    quantity a whole number, prices as numbers. "role" is ours and is never sent."""
    out = {}
    for k in KITE_PLACE_ORDER:
        v = lg.get(k)
        if v is None or v == "":
            continue
        if k in INT_PARAMS:
            out[k] = int(float(v))
        elif k in FLOAT_PARAMS:
            out[k] = float(v)
        else:
            out[k] = str(v)
    out.setdefault("variety", "regular")
    return out


def plain_leg(lg):
    """BUY 73 DEMO on NSE at market, intraday (MIS), good for today."""
    side = str(lg.get("transaction_type") or "?").upper()
    head = "%s %s %s on %s" % (side, _num(lg.get("quantity") or 0), str(lg.get("tradingsymbol") or "?").upper(),
                               str(lg.get("exchange") or "?").upper())
    typ = str(lg.get("order_type") or "MARKET").upper()
    if typ == "LIMIT":
        how = ", limit at %s" % inr(lg.get("price"))
    elif typ == "SL-M":
        how = ", stop-loss at market (SL-M) triggered at %s" % inr(lg.get("trigger_price"))
    elif typ == "SL":
        how = ", stop-loss limit (SL) triggered at %s, limit %s" % (inr(lg.get("trigger_price")), inr(lg.get("price")))
    else:
        how = " at market"
    prod = str(lg.get("product") or "").upper()
    val = str(lg.get("validity") or "DAY").upper()
    vw = VALIDITY_WORDS.get(val) or ("valid for %s minutes" % lg.get("validity_ttl") if val == "TTL" else val)
    return "%s%s, %s, %s" % (head, how, PRODUCT_WORDS.get(prod, prod or "?"), vw)


def max_loss(group):
    """(rupees, stop): the plan's max loss at the stop (charges included), else qty x (entry - stop)."""
    st = leg(group, "stop")
    plan = group.get("plan") or {}
    stop = _f((st or {}).get("trigger_price")) or _f(plan.get("stop"))
    if stop is None:
        return None, None
    if _f(plan.get("max_loss")) is not None:
        return round(_f(plan["max_loss"]), 2), stop
    en = leg(group, "entry") or {}
    entry = _f(en.get("price")) if str(en.get("order_type", "")).upper() == "LIMIT" else _f(plan.get("entry"))
    q = group_qty(group)
    if not entry or not q:
        return None, stop
    return round(q * (entry - stop), 2), stop


def plain(group):
    parts = ["%s: %s" % (r, plain_leg(lg)) for r, lg in role_legs(group)]
    loss, stop = max_loss(group)
    if loss is not None:
        parts.append("max loss about %s at the stop" % inr(loss))
    return " / ".join(parts)


def amount_numbers(group):
    nums = []
    for _, lg in role_legs(group):
        for k in ("quantity", "price", "trigger_price"):
            if _f(lg.get(k)) is not None:
                nums.append(_f(lg[k]))
    plan = group.get("plan") or {}
    for k in ("entry", "stop", "target", "max_loss", "risk_per_share"):
        if _f(plan.get(k)) is not None:
            nums.append(_f(plan[k]))
    loss, _ = max_loss(group)
    if loss is not None:
        nums.append(loss)
    return nums


def _hit(n, want):
    return any(abs(n - w) < 0.005 for w in want)


def parse_yes(reply, group, batch=()):
    """Is this reply an explicit yes for THIS one group? It must start with yes and name the
    stock and the quantity. Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one group at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym, qty = group_symbol(group), group_qty(group)
    say = "yes %s %s" % (qty, sym)
    tokens = set(re.findall(r"[A-Z0-9][A-Z0-9&\-]*", raw.upper()))
    others = {group_symbol(g) for g in batch if g is not group} - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this group is for %s" % (", ".join(sorted(named)), sym)
    if sym not in tokens:
        return False, "the reply must name the stock and the quantity, like \"%s\"" % say
    nums = [float(n.replace(",", "")) for n in NUM_RE.findall(raw)]
    if qty is None or not _hit(float(qty), nums):
        return False, "the reply must say the quantity, like \"%s\"" % say
    want = amount_numbers(group)
    wrong = [n for n in nums if not _hit(n, want)]
    if wrong:
        return False, "the reply says %s, but this group is %s" % (", ".join(_num(n) for n in wrong), plain(group))
    for g in batch:
        if g is group or group_symbol(g) != sym or group_qty(g) != qty:
            continue
        theirs = amount_numbers(g)
        if all(_hit(n, theirs) for n in nums):
            return False, ("the reply is ambiguous: group %s is also %s %s. Add a price only this group has "
                           "(its stop or its target)" % (g.get("group", "?"), qty, sym))
    return True, "explicit yes for %s" % plain(group)


# ---------------------------------------------------------------- state and the risk verdict

def load_state(path):
    st = read_json(path, "broker state", required=False) or {}
    if not isinstance(st.get("orders"), list):
        st["orders"] = []
    return st


def latest(state, fp, today, statuses=None):
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today and (statuses is None or rec.get("status") in statuses):
            found = rec
    return found


def _parse_time(text, naive_tz):
    if not text:
        return None
    t = str(text).strip().replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=naive_tz)
    return dt.astimezone(_UTC)


def risk_verdict(risk, fp, today, now_real):
    """(verdict record, None) when usable, else (None or record, (label, why))."""
    if not isinstance(risk, dict):
        return None, ("NOT READY", "nse-risk has not checked these orders (run nse-risk/check.py)")
    v = (risk.get("verdicts") or {}).get(fp)
    if not isinstance(v, dict):
        return None, ("NOT READY", "this exact group is not in today's risk check (it changed or is new): "
                                   "run nse-risk again")
    if v.get("date") != today:
        return v, ("RECHECK", "the risk check is from %s, not today (%s): run nse-risk again" % (v.get("date"), today))
    limit = _dt.timedelta(minutes=FRESH_MINUTES)
    checked = _parse_time(v.get("checked_at") or risk.get("checked_at"), _UTC)
    if checked is not None and abs(now_real - checked) > limit:
        mins = int(abs(now_real - checked).total_seconds() // 60)
        return v, ("RECHECK", "the risk check is %d minutes old (the limit is %d): run nse-risk again"
                   % (mins, FRESH_MINUTES))
    ran = _parse_time(v.get("now_ist") or v.get("now"), IST)
    if ran is not None and abs(now_real - ran) > limit:
        return v, ("RECHECK", "the risk check ran with a pinned clock (%s IST, not now): run nse-risk again"
                   % v.get("now_ist", v.get("now")))
    return v, None


def status_of(group, risk, state, today, now_real=None):
    """(ready, label, verdict record)"""
    fp = fingerprint(group)
    v, bad = risk_verdict(risk, fp, today, now_real or real_now())
    if bad:
        return False, "%s: %s" % bad, v
    if v.get("verdict") != "PASS":
        return False, "BLOCKED by nse-risk: " + "; ".join(v.get("reasons") or ["no reason recorded"]), v
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting for the order calls" % rec.get("at"), v
        return False, "DONE: %s at %s" % (st, rec.get("at")), v
    when = v.get("now_ist", "")[11:16]
    return True, "READY: nse-risk PASS%s, waiting for your yes" % (" at %s IST" % when if when else ""), v


# ---------------------------------------------------------------- rendering

def _json_lines(obj, pad):
    return [pad + l for l in json.dumps(obj, indent=2, ensure_ascii=False).splitlines()]


def render(group, idx, total, cfg, risk, state, today):
    broker = broker_of(cfg)
    ready, label, v = status_of(group, risk, state, today)
    acct = ("PAPER, local paper engine, no real money" if broker == "paper"
            else "ZERODHA KITE, LIVE ACCOUNT, REAL MONEY")
    lines = ["ORDER GROUP %d of %d: %s, %s" % (idx, total, group.get("group", "?"), acct)]
    rl = role_legs(group)
    for n, (role, lg) in enumerate(rl, 1):
        lines.append("  leg %d, %s: %s" % (n, role, plain_leg(lg)))
        lines.append("    place_order with exactly:")
        lines.extend(_json_lines(tool_input(lg), "      "))
    loss, stop = max_loss(group)
    if loss is not None:
        lines.append("  max loss about %s if the stop fills at %s, charges included (a fast market or a gap "
                     "can fill it worse)" % (inr(loss), inr(stop)))
    if group.get("why"):
        lines.append("  why: " + group["why"])
    if len(rl) > 1:
        lines.append("  Order of operations: Kite has no bracket orders for retail, so these are separate orders.")
        lines.append("    1. Place the entry leg.")
        lines.append("    2. Place the stop-loss and the target only after the entry fills.")
        lines.append("    3. When the stop or the target fills, cancel the other one (ticket.py fill prints the")
        lines.append("       cancel_order call). Square off anything still open by flat_by.")
    if broker == "paper":
        lines.append("  Paper mode: paper.py plays these legs on real 5-minute bars. Nothing goes to a broker.")
    lines.append("  " + label)
    if ready:
        lines.append("  Reply \"yes %s %s\" to place this one group, or \"no\" to skip it."
                     % (group_qty(group), group_symbol(group)))
        if broker == "kite":
            lines.append("  Claude's Needs approval card then asks again before each place_order call.")
    return "\n".join(lines), ready


def ticket_json(group, idx, cfg, risk, state, today):
    broker = broker_of(cfg)
    ready, label, v = status_of(group, risk, state, today)
    loss, stop = max_loss(group)
    return {"index": idx, "group": group.get("group"), "ready": ready, "status": label, "plain": plain(group),
            "fingerprint": fingerprint(group), "broker": broker, "paper": broker == "paper", "tool": "place_order",
            "legs": [{"role": r, "plain": plain_leg(lg), "tool_input": tool_input(lg)} for r, lg in role_legs(group)],
            "why": group.get("why", ""), "max_loss": loss, "stop": stop}


# ---------------------------------------------------------------- fills -> portfolio

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date):
    """Record a fill in nse/portfolio.json: cash and shares, a cost lot per buy (oldest relieved first)."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = (cfg or {}).get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "positions": []}
    symbol = symbol.upper()
    qty, price = float(qty), float(price)
    if qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive qty and price")
    cost = round(qty * price, 2)
    pos = None
    for p in pf.setdefault("positions", []):
        if str(p.get("ticker") or p.get("symbol") or "").upper() == symbol:
            pos = p
    if side == "buy":
        if pos is None:
            pos = {"ticker": symbol, "shares": 0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": qty, "cost": cost})
        pos["shares"] = round(float(pos.get("shares", 0)) + qty, 6)
        pf["cash"] = round(float(pf.get("cash", 0)) - cost, 2)
    else:
        if pos is None or float(pos.get("shares", 0)) + 1e-9 < qty:
            raise TicketError("cannot record selling %s %s: the portfolio holds %s"
                              % (_num(qty), symbol, pos.get("shares", 0) if pos else 0))
        left = qty
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= 1e-9:
                break
            take = min(left, float(lot["shares"]))
            per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
            lot["cost"] = round(float(lot["cost"]) - per * take, 2)
            lot["shares"] = round(float(lot["shares"]) - take, 6)
            left -= take
        pos["lots"] = [l for l in pos.get("lots", []) if float(l["shares"]) > 1e-9]
        pos["shares"] = round(float(pos["shares"]) - qty, 6)
        if pos["shares"] <= 1e-9:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf.get("cash", 0)) + cost, 2)
    write_json(portfolio_path, pf)
    return pf


# ---------------------------------------------------------------- commands

def _clock(a):
    try:
        now = parse_now(getattr(a, "now", None))
    except ValueError as e:
        raise TicketError(str(e))
    return now, to_ist(now).date().isoformat()


def _groups(path):
    doc = read_json(path, "orders file (run nse-budget, or ticket.py flatten, first)")
    groups = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(groups, list) or not groups or not all(isinstance(g, dict) for g in groups):
        raise TicketError("no order groups in %s" % path)
    return groups


def _load(a):
    groups = _groups(a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    return groups, cfg, risk, state, today, now


def _pick(groups, index):
    if index is None:
        raise TicketError("say which group: --index N")
    if not 1 <= index <= len(groups):
        raise TicketError("there is no group %d (there are %d)" % (index, len(groups)))
    return groups[index - 1]


def cmd_show(a):
    groups, cfg, risk, state, today, now = _load(a)
    broker_of(cfg)
    picks = [(a.index, _pick(groups, a.index))] if a.index else list(enumerate(groups, 1))
    if a.json:
        out = [ticket_json(g, i, cfg, risk, state, today) for i, g in picks]
        print(json.dumps(out[0] if a.index else out, indent=2, ensure_ascii=False))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, g in picks:
        text, ready = render(g, i, len(groups), cfg, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne group at a time: a yes covers one group, never the batch. Not financial advice: education "
          "and tooling.")
    return 0 if any_ready else 1


def cmd_approve(a):
    groups, cfg, risk, state, today, now = _load(a)
    broker = broker_of(cfg)
    group = _pick(groups, a.index)
    ready, label, v = status_of(group, risk, state, today)
    if not ready:
        raise TicketError("group %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.reply, group, groups)
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for group %d." % (why, a.index))
        return 1
    loss, stop = max_loss(group)
    main = main_leg(group)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "group": group.get("group"),
           "fingerprint": fingerprint(group), "symbol": group_symbol(group),
           "side": str(main.get("transaction_type", "")).upper(), "qty": group_qty(group),
           "kind": (v or {}).get("kind") or ("entry" if leg(group, "entry") else "exit"), "max_loss": loss,
           "stop": stop, "plain": plain(group), "status": "approved", "reply": a.reply,
           "paper": broker == "paper", "broker": broker}
    state["orders"].append(rec)
    write_json(a.state, state)
    print("Recorded your yes for group %d (%s): %s." % (a.index, group.get("group"), plain(group)))
    if broker == "paper":
        print("Paper mode: no broker is called. Run the paper engine for this group:")
        print("  python3 nse-broker/paper.py run --group %s --orders %s" % (group.get("group"), a.orders))
        print("It fills the entry at the open of the first 5-minute bar from now, then plays the stop, the")
        print("target and the flat_by square-off, and records the fills and the P&L net of charges.")
        return 0
    first_role, first = role_legs(group)[0]
    print("Now call place_order (Kite MCP, set to Needs approval) for the %s leg with exactly:" % first_role)
    print(json.dumps(tool_input(first), indent=2, ensure_ascii=False))
    print("Then record it: ticket.py result --index %d --leg %s --status placed --order-id <Kite's order id>"
          % (a.index, first_role))
    if len(role_legs(group)) > 1:
        print("Do not place the stop-loss or the target yet. Only after the entry fills (get_orders or")
        print("get_order_history shows COMPLETE), record it with ticket.py fill --group %s --leg entry --price P;"
              % group.get("group"))
        print("it prints their place_order calls.")
    return 0


def cmd_decline(a):
    groups, cfg, risk, state, today, now = _load(a)
    group = _pick(groups, a.index)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "group": group.get("group"),
           "fingerprint": fingerprint(group), "symbol": group_symbol(group), "plain": plain(group),
           "status": "declined", "reply": a.reply or "no"}
    state["orders"].append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to group %d (%s). Nothing was sent." % (a.index, group.get("group")))
    return 0


def cmd_result(a):
    groups, cfg, risk, state, today, now = _load(a)
    group = _pick(groups, a.index)
    fp = fingerprint(group)
    appr = latest(state, fp, today, ("approved",))
    if not appr:
        raise TicketError("group %d has no yes recorded today; a result can only follow an approval" % a.index)
    lg = leg(group, a.leg)
    if lg is None:
        raise TicketError("group %d has no %s leg" % (a.index, a.leg))
    if a.leg in ("stop", "target") and a.status == "placed":
        filled = [r for r in state["orders"] if r.get("fingerprint") == fp and rec_day(r) == today
                  and r.get("status") == "filled" and r.get("leg") == "entry"]
        if not filled:
            raise TicketError("the entry leg has not filled yet: place the stop-loss and the target only after "
                              "the entry fills (ticket.py fill --group %s --leg entry --price P)" % group.get("group"))
    new = {"at": ist_iso(now), "date": today, "index": a.index, "group": group.get("group"), "fingerprint": fp,
           "leg": a.leg, "symbol": group_symbol(group), "side": str(lg.get("transaction_type", "")).upper(),
           "qty": int(float(lg.get("quantity") or 0)), "plain": plain_leg(lg), "status": a.status,
           "broker": appr.get("broker"),
           "kind": appr.get("kind", "entry") if a.leg == "entry" else "exit"}
    if a.leg == "entry":
        new["max_loss"], new["stop"] = appr.get("max_loss"), appr.get("stop")
    if a.order_id:
        new["order_id"] = a.order_id
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    write_json(a.state, state)
    print("Recorded the %s leg of group %d as %s%s." % (a.leg, a.index, a.status,
                                                         (" (order id %s)" % a.order_id) if a.order_id else ""))
    if a.status == "placed":
        print("When it fills, record it: ticket.py fill --group %s --leg %s --price P" % (group.get("group"), a.leg))
    return 0


def _sibling_order_id(state, fp, role):
    found = None
    for r in state.get("orders", []):
        if r.get("fingerprint") == fp and r.get("leg") == role and r.get("order_id"):
            found = r["order_id"]
    return found


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    groups = _groups(a.orders)
    match = [g for g in groups if g.get("group") == a.group]
    if not match:
        raise TicketError("no group %s in %s" % (a.group, a.orders))
    group = match[0]
    lg = leg(group, a.leg)
    if lg is None:
        raise TicketError("group %s has no %s leg" % (a.group, a.leg))
    now, today = _clock(a)
    qty = _f(a.qty) if a.qty is not None else _f(lg.get("quantity"))
    price = _f(a.price)
    if not qty or qty <= 0 or qty != int(qty) or not price or price <= 0:
        raise TicketError("a fill needs a whole positive --qty and a positive --price")
    qty = int(qty)
    side = str(lg.get("transaction_type", "")).upper()
    sym = group_symbol(group)
    fp = fingerprint(group)
    state = load_state(a.state)
    apply_fill(a.portfolio, cfg, sym, side.lower(), qty, price, today)
    state["orders"].append({"at": ist_iso(now), "date": today, "status": "filled", "group": a.group,
                            "fingerprint": fp, "leg": a.leg, "symbol": sym, "side": side, "qty": qty,
                            "price": price, "plain": "%s %d %s at %s (%s)" % (side, qty, sym, inr(price), a.leg)})
    write_json(a.state, state)
    print("Recorded the %s fill: %s %d %s at %s on %s." % (a.leg, side, qty, sym, inr(price), today))
    if a.leg == "entry":
        rest = [(r, l) for r, l in role_legs(group) if r in ("stop", "target")]
        if rest:
            print("Entry filled: place the stop-loss and the target now, each with its own place_order call")
            print("(Needs approval). They are separate orders, so watch them: when one fills, cancel the other.")
            for r, l in rest:
                call = tool_input(l)
                call["quantity"] = qty
                print("%s leg, place_order with exactly:" % r)
                print(json.dumps(call, indent=2, ensure_ascii=False))
        return 0
    if a.leg in ("stop", "target"):
        entry = [r for r in state["orders"] if r.get("fingerprint") == fp and r.get("status") == "filled"
                 and r.get("leg") == "entry"]
        if entry:
            gross = (price - float(entry[-1]["price"])) * qty
            print("Gross P&L about %s before charges." % inr(round(gross, 2)))
        other = "target" if a.leg == "stop" else "stop"
        sib = leg(group, other)
        if sib is not None:
            oid = _sibling_order_id(state, fp, other)
            print("The %s leg filled. Cancel the %s leg now, or it stays open and can sell again into a short:"
                  % (a.leg, other))
            if not oid:
                print("(ticket.py result recorded no order id for the %s leg: find it with get_orders, status OPEN"
                      % other)
                print("or TRIGGER PENDING, and put it in place of the placeholder.)")
            print("cancel_order with exactly:")
            print(json.dumps({"variety": str(sib.get("variety") or "regular"),
                              "order_id": oid or "<order_id of the %s leg>" % other}, indent=2))
    return 0


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    sym = a.symbol.strip().upper()
    now, today = _clock(a)
    source = "your --qty (from get_positions)"
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0 or qty != int(qty):
            raise TicketError("--qty wants the whole number of shares you hold, like --qty 73")
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        qty = sum(_f(p.get("shares", p.get("qty"))) or 0 for p in pf.get("positions", [])
                  if str(p.get("ticker") or p.get("symbol") or "").upper() == sym)
        source = a.portfolio
        if qty <= 1e-9:
            raise TicketError("nothing to flatten: no %s shares in %s. Check Kite (get_positions) and pass --qty "
                              "if you do hold some" % (sym, a.portfolio))
    qty = int(qty)
    plan = read_json(a.plan, "plan", required=False) or {}
    flat_by = (plan.get("flat_by") if str(plan.get("symbol", "")).upper() == sym else None) \
        or cfg.get("flat_by") or "15:10"
    lg = {"role": "exit", "exchange": str(cfg.get("exchange") or "NSE").upper(), "tradingsymbol": sym,
          "transaction_type": "SELL", "quantity": qty, "product": str(cfg.get("product") or "MIS").upper(),
          "order_type": "MARKET", "variety": "regular", "validity": "DAY"}
    group = {"group": "%s-flatten-%s" % (sym, today), "legs": [lg],
             "why": "flat by %s IST: closes the %s day trade before the MIS auto square-off (qty from %s)"
                    % (flat_by, sym, source),
             "plan": {"flat_by": flat_by}}
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "nse-broker", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "paper": broker_of(cfg) == "paper",
           "note": "Flatten ticket. Nothing is placed until nse-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [group]}
    write_json(a.orders, doc)
    print("Flatten %s: %s (flat by %s IST)." % (sym, plain_leg(lg), flat_by))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against get_positions before you say yes." % source)
    print("Next:")
    print("  1. If the stop-loss or the target leg is still open at Kite, cancel each one first with cancel_order")
    print("     {variety, order_id} (Needs approval; get_orders lists them). An open SL-M sell plus this sell")
    print("     would sell twice and leave you short.")
    print("  2. nse-risk: check.py. A sell that only closes what you hold is never blocked by the day limits.")
    print("  3. ticket.py show, then the user's yes for this one group (\"yes %d %s\")." % (qty, sym))
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + str(r["order_id"])) if r.get("order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r.get("status") in ("approved", "declined") else ""
        loss = (" max loss %s" % inr(r["max_loss"])) if r.get("max_loss") is not None and r.get("status") == "placed" \
            else ""
        pnl = (" net P&L %s" % inr(r["net_pnl"])) if r.get("net_pnl") is not None else ""
        print("%s  %-9s %s%s%s%s%s" % (r.get("at"), r.get("status"), r.get("plain"), reply, loss, pnl, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets and your yes (nse-broker). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="date the records as this IST time, e.g. \"2026-10-27 10:15\"")

    def common(p):
        p.add_argument("--orders", default="nse/orders.json")
        p.add_argument("--config", default="nse/config.json")
        p.add_argument("--risk", default="nse/state/risk.json")
        p.add_argument("--state", default="nse/state/broker.json")
        clock(p)

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one group")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="record what happened to one leg after the Needs approval card")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--leg", required=True, choices=list(ROLES))
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    p.add_argument("--order-id", help="Kite's order id from the place_order answer")
    p.add_argument("--note")
    p = sub.add_parser("fill", help="record a leg's fill; prints the next leg or the cancel ticket")
    p.add_argument("--group", required=True)
    p.add_argument("--leg", required=True, choices=list(ROLES))
    p.add_argument("--price", required=True)
    p.add_argument("--qty", help="default: the leg's quantity")
    p.add_argument("--orders", default="nse/orders.json")
    p.add_argument("--portfolio", default="nse/portfolio.json")
    p.add_argument("--config", default="nse/config.json")
    p.add_argument("--state", default="nse/state/broker.json")
    clock(p)
    p = sub.add_parser("flatten", help="a SELL ticket that closes a symbol's position by flat_by")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="shares you hold, from get_positions (default: nse/portfolio.json)")
    p.add_argument("--orders", default="nse/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="nse/portfolio.json")
    p.add_argument("--plan", default="nse/plan.json")
    p.add_argument("--config", default="nse/config.json")
    clock(p)
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default="nse/state/broker.json")
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "fill": cmd_fill, "flatten": cmd_flatten, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
