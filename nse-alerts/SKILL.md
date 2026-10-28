---
name: nse-alerts
description: >-
  Price Alerts for NSE. Rings when a price, a stop or the opening bell hits.
  Turns the trade plan into three alerts in one go (the stop, the target and
  "flat by" at your 15:10 IST flat-by time), plus above / below / stop /
  target levels, percent moves from the previous close, any IST time of day,
  and the 09:15 IST opening bell, each ringing once a day so you are not
  spammed. Rings when one exit leg has filled at the broker while its sibling
  is still open (an orphaned stop or target). Knows NSE hours and the 2026
  holiday list, can diff it against NSE's own feed, works in IST itself,
  fetches free delayed Yahoo quotes one symbol at a time, and writes
  nse/prices.json with the previous close for sizing and the risk check.
  Use when the user says "alert me when", "tell me if RELIANCE hits", "watch
  my stop", "watch my trade", "remind me to square off", "is the market
  open", "when does NSE open", "get me a price", or right after /nse-exit
  writes a plan.
argument-hint: "[symbol] [price]"
---

# nse-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. On Kite the entry, the SL-M stop and the LIMIT
target are separate orders, so the alerts also tell the user what happened
and when it is time to be flat or to cancel a leg that is still open.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open or closed, next open (IST)
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat by, from nse/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --stop 1150
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --target 1200
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:10 --label "flat by"
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol TCS --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE --write         # -> nse/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # portfolio + plan symbols
python3 "${CLAUDE_SKILL_DIR}/alerts.py" calendar                       # the holiday table in the tool
python3 "${CLAUDE_SKILL_DIR}/alerts.py" calendar --refresh             # diff it against NSE's feed
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset reliance-plan-stop
```

`--now "2026-10-28 15:10"` pins the clock (IST); `--offline DIR` reads saved
quote files (`yahoo-<SYMBOL>.json`) instead of the network. Symbols are NSE
symbols: `M&M`, `BAJAJ-AUTO` and the rest are encoded for Yahoo by the tool.

## What it reads and writes

| file | what |
| --- | --- |
| `nse/plan.json` | the trade from nse-exit, for `add --from-plan` |
| `nse/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `nse/state/alerts.json` | which rules rang and when, so each rings once a day |
| `nse/state/broker.json` | read by `check` to find an orphaned exit leg (nse-broker writes it) |
| `nse/prices.json` | the latest quotes, merged in, for nse-budget and nse-risk |
| `nse/portfolio.json` | the symbols held, for `quote --holdings` |
| `nse/config.json` | `flat_by` when the plan has none; `special_sessions` |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (Yahoo's own, or `ref`), optional `direction` up or down | once per day at most |
| `time` | the IST clock passed `at` ("15:10") on a trading day | once per trading day |
| `opening_bell` | the NSE cash market opened at 09:15 IST | once per trading day; never on a weekend or holiday |
| `orphan_leg` | automatic, nothing to add: see below | once per day per leg |

A rule with `"date"` is watched on that IST date only; `--from-plan` rules
carry the plan's date, so yesterday's stop never rings today. A special
session (the Diwali Muhurat evening, set in `special_sessions` in the config)
rings the bell at its own start.

## The orphan leg

Kite has no bracket orders for retail, so the stop-loss and the target sit at
the broker as two separate orders. When one fills, the other is still live and
could sell again, into a short. `check` reads `nse/state/broker.json`: for
each group whose stop, target or exit (flatten) leg is `filled` today, while
the sibling leg was `placed` and has no `cancelled` or `filled` record, it
rings once: "RELIANCE: target filled, cancel the stop leg now". The cancel_order
ticket itself was printed by `nse-broker/ticket.py fill`; use that. The alert
never cancels anything.

## Quotes

- Yahoo only: `query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS`, an
  unofficial free JSON endpoint, one request per symbol, half a second apart.
  On a 429 it waits and tries once more; on a 429 or a 5xx after that it tries
  `query1`. It identifies itself as `Mozilla/5.0 (nse-day-trader-skill)`. There
  is no second feed: no free CSV source has reliable NSE coverage.
- Free quotes are unofficial, delayed and can lag. They are fine for alerts
  and for checking a size, not for timing an order. The broker's price is the
  one that fills.
- nse-risk needs a previous close for any entry: run `quote RELIANCE --write`
  first. It writes `nse/prices.json` as `{"as_of", "source", "currency":
  "INR", "note", "prices": {SYM: price}, "detail": {SYM: {"prev_close",
  "time", "source", "fetched"}}}`, with `prev_close` from Yahoo's chart meta
  and `time` the quote's own time. nse-risk accepts a previous close only when
  that time is today (IST).
- If Yahoo is down, read the price from the Kite connector and write it into
  `nse/prices.json` by hand, with a `prev_close` from the broker's quote.

## The NSE clock

Pre-open 09:00 to 09:08, normal market 09:15 to 15:30 IST, closed on weekends
and on the NSE CM holidays. For 2026, from NSE's holiday-master feed (key
`CM`, fetched 2026-10-28), weekdays only: Jan 15 (Maharashtra civic
election), Jan 26, Mar 3, Mar 26, Mar 31, Apr 3, Apr 14, May 1, May 28,
Jun 26, Sep 14, Oct 2, Oct 20, Nov 10, Nov 24, Dec 25. IST has no daylight
saving, so there is nothing to work out.

For any other year the tool treats Mon-Fri as open and prints `NSE calendar
for <year> not loaded: run nse-alerts/alerts.py calendar --refresh or check
nseindia.com`. `calendar --refresh` fetches
`https://www.nseindia.com/api/holiday-master?type=trading`, prints the CM
holidays for the year (weekdays only) and the difference against the table in
the tool (added, removed, renamed). It never edits code; exit 0 means no
differences. `--year 2027` picks the year, `--offline DIR` reads
`DIR/holiday-master.json`. NSE sometimes turns scripts away; then compare by
hand on nseindia.com.

## Steps

1. Right after nse-exit writes the plan, run `add --from-plan`. Confirm the
   three alerts back to the user: the stop, the target and the flat-by time.
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`. Each run fetches only the symbols in today's
   rules and rings each alert once a day. A check reads the latest delayed
   quote; it is not a live feed, and a price can pass a level between checks.
3. Tell the user what rang in one line each. A stop or target alert means that
   leg has probably filled: check the broker's positions and record the fill
   with nse-broker (`ticket.py fill`), which prints the cancel ticket for the
   other leg. "Flat by" means close anything still open: nse-broker
   `flatten`, then nse-risk, then the user's yes. Never act for them.

## Example: RELIANCE on 2026-10-28

Plan: entry 1180.00, stop 1150.00, target 1200.00, flat by 15:10 IST.
`add --from-plan` writes `reliance-plan-stop`, `reliance-plan-target` and
`reliance-flat-by` for that day. A `check` at 10:30 AM IST reads the delayed
quote, 1158.00 against a previous close of 1196.20, between the stop and the
target:

```
NSE OPEN (2026-10-28 10:30 IST).
Nothing hit. 4 rule(s) watched today, RELIANCE ₹1,158.00.
```

At 03:10 PM IST:

```
ALERT Flat by: it is 15:10 IST (set for 15:10), the market closes at 15:30. If RELIANCE is still open,
close it: nse-broker/ticket.py flatten --symbol RELIANCE builds the sell ticket, then nse-risk and your yes.
```

It rings once. The next morning the plan's three alerts are no longer
watched; with `add --opening-bell`, the bell rings at 09:15 IST.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as live. Free quotes are delayed and unofficial.
- Confirm every new rule back to the user: symbol, type, level, day.

## What it never does

It never places, cancels or changes an order, never claims a quote is live,
and never predicts where a price is going.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No F&O, leverage or short selling unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
- SEBI's own studies found most individual intraday traders lose money. An alert does not change that.
