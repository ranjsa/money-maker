---
name: nse-risk
description: >-
  Risk Manager for NSE. Checks every order group against your limits and
  stops anything over them, before it can reach the broker. For an intraday
  (MIS) trade: EQ series only (BE, BZ and SME stocks blocked), no entry near
  the upper circuit or in a 2% / 5% band stock, all three Kite legs present
  and consistent (entry, SL-M stop, LIMIT target, same quantity, MIS), the
  loss at the stop including brokerage, STT, GST and the other charges must
  fit your max loss per trade, reward:risk must meet your minimum, the day's
  total risk must fit your daily loss limit, no more than your trades per
  day, and no new entries outside your IST entry window or on an NSE
  holiday. It also keeps the basics: blocked symbols, order size, position
  cap, cash buffer, leverage, F&O, short sales, duplicates and a daily order
  limit. Getting out is never blocked by a day limit. Prints PASS or BLOCK
  with the reason and records the verdicts so nse-broker only shows an order
  that passed. Use when the user says "check my order", "is this safe",
  "risk check", "does this break my rules", "can I take this trade", "is it
  near the upper circuit", "how much have I risked today", or before
  /nse-broker, always.
argument-hint: "[path to orders.json]"
---

# nse-risk

The Risk Manager is the gate between a proposal and the broker. Every order
group written by nse-budget (or nse-broker's flatten) passes through it. A
BLOCK is final for that group: it does not go to the user for a yes, and it
does not go to the broker. Most people who trade intraday lose money (SEBI's
studies say so, see the README). These limits keep each loss and each day's
losses small. They do not make a trade win. Not financial advice.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py" --refresh-bands             # once a day: NSE price bands, then the check
python3 "${CLAUDE_SKILL_DIR}/check.py"                             # nse/ files; writes nse/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                  # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-10-27 10:15"    # pin the clock (IST)
```

## What it reads and writes

| file | what |
| --- | --- |
| `nse/orders.json` | the proposed order groups (nse-budget: entry, SL-M stop and LIMIT target legs in Kite field names) |
| `nse/config.json` | the limits (nse-budget ships `config.example.json`) |
| `nse/portfolio.json` | cash and holdings; if missing, a fresh account funded with the budget |
| `nse/prices.json` | a fresh price raises the entry it measures risk from; `detail.SYM.prev_close` sets the circuit band |
| `nse/state/broker.json` | what was placed today: duplicates, trades per day, daily loss, orders per day |
| `nse/state/bands.json` | NSE's `sec_list.csv` (series and price band of every stock), cached by `--refresh-bands` |
| `nse/universe.json` | the EQ list from nse-scanner, for a stock the band file does not list |
| `nse/state/risk.json` | the verdicts it writes, keyed by the fingerprint (sha256 of the legs) of each exact group |

`--refresh-bands` fetches `https://nsearchives.nseindia.com/content/equities/sec_list.csv`
(`--offline DIR` reads `DIR/sec_list.csv` instead). Bands change, so refresh
once a trading day. If the bands are missing or older than today and a group
is an entry, the check tries one live refresh itself (never with `--offline`);
if that fails, entries are blocked (circuit) until you run `--refresh-bands`.

## Entries and exits

An **entry** group opens or adds to a position: a BUY entry leg, or a sell of
more than you hold. An **exit** group only reduces what you already hold: SELL
legs whose quantity fits inside the holding (a stop and a target for the same
shares count once, since only one of them can fill). Getting out must always
be possible, so an exit is checked for format, F&O, series and short sales
only. Every other rule (window, day limits, loss, legs, blocked list, size,
duplicates) turns into a warning for an exit, never a BLOCK.

## The checks

Groups are checked in file order, as if each passed group had filled, so a
batch that is fine group by group but breaks a limit together is caught.

| check | blocks when |
| --- | --- |
| format | no legs; legs on different symbols or exchanges; a side that is not BUY or SELL; a quantity that is not a whole number; an unknown `order_type`, `variety` or `validity`; a LIMIT or SL leg with no `price`; an SL or SL-M leg with no `trigger_price` |
| fno | a leg on NFO or BFO while `allow_fno` is false |
| series | the stock is not in the EQ series (BE and BZ are trade-to-trade, SM / ST are SME: no intraday). From the band file, else `nse/universe.json`; a stock in neither is blocked, "not in the EQ universe" (a warning for an exit). Only with neither file loaded is it a warning, "series unknown" |
| short | a sell of more than you hold (`allow_short` false) |
| legs | (entries) no stop leg while `require_stop` is true; legs with different quantities; a stop leg that is not SL-M or SL; a product other than MIS; an exchange other than NSE or BSE; a stop trigger at or above the entry; a target at or below the entry |
| circuit | (entries) the entry within 3 ticks of the upper band, previous close x (1 + band%); a stock with a 2% or 5% band unless `allow_banded` is true; price bands missing or older than today; no previous close dated today in `nse/prices.json` (run `nse-alerts/alerts.py quote SYM --write`). A stock missing from a fresh band file is a warning. F&O stocks have no fixed band |
| blocked | (entries) the symbol is on the `blocked` list |
| size | (entries) under `min_order_inr` or over `max_order_inr` |
| position | (entries) the stock would be over `max_position_pct` of the account |
| cash | (entries) the buy would leave less than the cash buffer (`cash_buffer_pct`) |
| leverage | (entries) the buy costs more than your cash while `allow_leverage` is false (MIS margin is not modelled) |
| loss | (entries) qty x (entry minus stop) plus the round-trip charges at the stop over `max_loss_per_trade_inr` |
| reward:risk | (entries) (target minus entry) / (entry minus stop) under `min_reward_risk` |
| daily loss | (entries) the max losses at the stops of today's entries, plus this one, over `max_daily_loss_inr` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) NSE closed (weekend, NSE holiday by name, pre-open, before 09:15 or after 15:30), before `no_entries_before` (09:30) or after `no_entries_after` (14:45), IST |
| duplicate | (entries) the same symbol and side twice in one batch, or already placed today |
| daily limit | (entries) `max_orders_per_day` Kite orders (one per leg) already placed today, counting this group's legs |

How the numbers work:

- **The entry it measures from**: the limit price for a LIMIT entry. For a
  MARKET entry, the HIGHER of the plan's entry and a fresh price from
  `nse/prices.json` (under 15 minutes old). A plan written at 09:35 cannot
  understate the risk at 10:30 after the price ran up. An older price is not
  used, and it says so.
- **The loss at the stop** is net: 73 shares x ₹9.80 = ₹715.40 plus ₹73.26
  round-trip charges (brokerage, STT, exchange charges, SEBI fee, stamp duty,
  GST) = ₹788.66. On a tight stop the charges can be a big part of the risk.
- **Daily loss** counts the full max loss of every entry placed today, win or
  lose, as nse-broker logged it.
- **The circuit band**: a stock locked at its upper circuit has buyers only,
  so a long bought near it may not be sellable at your stop. The check uses
  NSE's band from `sec_list.csv` and the previous close from
  `nse/prices.json`; the tick is the NSE tick for the entry price.
- **The window** is IST, worked out in the tool, never from the computer's
  clock zone. NSE holidays come from the shared calendar (2026 loaded); on
  2026-11-24 the reason reads "NSE is closed today (Prakash Gurpurb Sri Guru
  Nanak Dev...)". For a year outside the calendar it treats Mon-Fri as open
  and warns. A special session in `special_sessions` (such as Muhurat
  trading) counts as open within its own times, and the 09:30 to 14:45
  entry window does not apply to it. Set both `no_entries_*` to null to
  switch the window off.

Warnings do not block, but say them to the user: a LIVE account, a stop that
lives only in the plan and not at the broker, a price too old to use, no
band for a stock in a fresh band file, a series when neither list is loaded,
a price off the tick.

## Steps

1. Once a trading day, refresh the bands: `check.py --refresh-bands`.
2. If the price may have moved since the plan, refresh it first
   (nse-alerts: `alerts.py quote DEMO --write`). The circuit check reads the
   previous close from `detail.DEMO.prev_close` in `nse/prices.json`, and only
   if that quote's time (`detail.DEMO.time`, else the file's `as_of`) is today.
3. Run `check.py`. Exit code 0 means every group passed, 1 means at least one
   was blocked.
4. Tell the user the result in plain words, group by group: the max loss at
   the stop with charges, reward:risk, and how much of today's limits it
   uses. For a BLOCK, give the reason and the fix (fewer shares, a closer
   stop from nse-exit, wait for the window, or skip the trade). Never change
   a limit yourself.
5. If the group changes in any way, run the check again. nse-broker matches
   each group to its verdict by fingerprint, so an edited group is not ready
   until it passes again, and a verdict from yesterday does not count today.

## Example: DEMO

The DEMO group from nse-budget at 10:00 AM IST on Tuesday 2026-10-27, on a
fresh ₹1,00,000 paper account with the example config:

```
PASS  #1 DEMO-2026-10-27-1: BUY 73 DEMO, entry
          entry ₹1,015.00 (the plan's entry), stop ₹1,005.20, target ₹1,034.60: risk ₹9.80 a share
          max loss ₹788.66 with ₹73.26 charges (limit ₹1,000.00), reward:risk 2.00 (min 1)
          today before this: 0 of 3 trades, ₹0.00 of ₹3,000.00 daily loss

1 passed, 0 blocked at 2026-10-27 10:00 IST. Cash after the passed orders: ₹25,905.00 (paper account).
```

The same group at 2:50 PM IST is blocked: `window: no new entries after
14:45 IST (it is 14:50)`. On 2026-11-24 it is blocked because NSE is closed
for Prakash Gurpurb Sri Guru Nanak Dev. With a previous close of ₹846.00 the
upper band (20%) is ₹1,015.20 and the entry is within 3 ticks of it: blocked,
circuit. The 3:25 PM market sell of the 73 shares passes, even though it is
after the 14:45 entry cutoff: it is an exit.

## Rules

- A PASS only means the group is allowed to be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices, the bands or the broker log.
- Free Yahoo prices are unofficial and can lag; the broker's price is the one that fills, and an
  SL-M stop can fill worse than its trigger in a fast market or not at all at a circuit.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out, and never
treats a PASS as the user's yes.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact group today, the user typed an explicit yes for that one group in the chat, and
  the user clicked Allow once on Claude's own card because the order tool is set to "Needs approval".
  Never set an order tool to "Always allow".
- No F&O, leverage or short selling unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
