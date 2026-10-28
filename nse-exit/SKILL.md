---
name: nse-exit
description: >-
  Exit Planner for NSE. Sets your stop and your target before you enter, so
  you know when to get out. Reads the entry from nse-entry (or a price you
  give), puts the stop at the opening-range midpoint by default (or the range
  low, your price, a percent, or a multiple of the ATR), rounds it down to the
  NSE tick, and sets the target at 2R (or 1R, or your price). Estimates the
  round-trip charges per share and prints the net R after them. Refuses a
  stop at or above the entry, a target at or below it, a stop under 3 ticks,
  or charges over half the risk, and says why. Writes nse/plan.json for the
  Time Machine, the Budget Manager and Price Alerts. Long side only, intraday
  MIS. Use when the user says "where's my stop", "set my stop and target",
  "plan the exit", "when do I get out", "stop loss", "what's my R", "risk
  reward", or right after /nse-entry triggers, before /nse-timemachine.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X]"
---

# nse-exit

The Exit Planner decides how a trade ends before it starts. The stop is where
the setup has failed. The target is where you take what you came for. Both
are set BEFORE the entry, written down and handed on, so nobody has to decide
anything in the middle of a fast move. A stop is a plan, not a guarantee: a
gap or a fast market can fill past it. Day trading loses most people money,
and a neat plan does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # nse/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 2R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs the session's bars (Yahoo or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol RELIANCE --entry 1215 --or-high 1212.4 --or-low 1198.1
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current nse/plan.json
```

Other flags: `--flat-by 15:10`, `--json`, `--now "2026-10-27 09:36"` (IST),
`--entry PRICE` with a waiting entry.json to plan ahead, `--setup level`,
`--offline FILE` (a saved Yahoo chart JSON for an ATR stop).

## What it reads and writes

| file | what |
| --- | --- |
| `nse/entry.json` | the trigger from nse-entry: symbol, entry price, time in IST, bar, opening range |
| `nse/config.json` | `flat_by` (15:10), `min_reward_risk` (1.0: a note here, nse-risk enforces it), optional `charges` overrides |
| Yahoo chart API | only for an `atr:K` stop: the session's bars up to the entry bar, free and unofficial |
| `nse/plan.json` | with `--write`: entry, stop and its rule, target and its R, risk a share, reward:risk, tick, estimated charges a share, net R, flat by, `"exchange": "NSE"`, `"product": "MIS"`. nse-timemachine, nse-budget and nse-alerts read it |

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | your price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar. With fewer than 14 bars by then it averages all of them and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

- **Tick rounding.** NSE prices move in ticks that depend on the price: 0.01
  under 250, 0.05 from 250 to 1000, 0.10 over 1000 to 5000, 0.50 over 5000 to
  10000, 1.00 over 10000 to 20000, 5.00 above. The entry price picks the tick.
  The stop rounds DOWN to the tick (away from the entry, for a long). Risk =
  entry minus stop. Target = entry + R x risk, rounded to the nearest tick but
  never toward the entry. `2R` by default; `1R`, `1.5R` or `price:X` also work.
- **Charges and net R.** A day trade pays brokerage, STT, exchange and SEBI
  fees, stamp duty and GST, both ways. The plan estimates them a share
  (round trip at 100 shares; nse-budget recomputes at the real quantity) and
  prints Net R = (reward - charges) / (risk + charges). On a cheap stock or a
  tight stop the net R is well under the gross R. Rates are in `charges` in
  the config if your broker differs.
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A stop under 3 ticks from the entry: too tight to survive the
  spread, normal wiggle would stop you out. Charges over half the risk a share:
  "widen the stop or skip". An entry.json that has not triggered (unless you
  give `--entry`). A short: v1 plans longs only. No plan file is written on a
  refusal.
- **Flat by.** 15:10 IST by default (or `flat_by` in the config). Out by then,
  win or lose, no overnight hold. Brokers may auto square-off MIS from about
  15:20, so a flat-by later than 15:20 gets a warning. An entry at or after the
  flat-by time is refused. A holiday or a year outside the calendar is flagged.

## Steps

1. Run it right after `/nse-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule,
   the target and its rule, the risk a share, reward:risk, the charges, the
   net R and flat by.
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target). Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/nse-timemachine` to replay the plan, then `/nse-budget`
   to size it to the max loss per trade. Offer `/nse-alerts` for the stop,
   the target and the flat-by time. Claude can keep `entry.py watch` running
   in the background or on a `/loop` until a setup triggers, then come here.

## Example: the demo

DEMO (synthetic test data, not a real stock), Tuesday 2026-10-27. The entry is
₹1,015.00 at 09:35 AM IST (the 09:30 bar, bar 3) and the opening range is
₹998.10 to ₹1,012.40. `exit.py plan --stop or-mid --target 2R --write`:

```
Exit plan: DEMO long (NSE, MIS), Tue 2026-10-27, opening-range breakout on 5-minute bars
  Entry         ₹1,015.00 at 09:35 AM IST (bar 3)
  Stop          ₹1,005.20  opening-range midpoint: (₹1,012.40 + ₹998.10) / 2 = ₹1,005.25, rounded down to the 0.10 tick
  Target        ₹1,034.60  2R (two times the risk)
  Risk a share  ₹9.80 (0.97% of the entry, tick ₹0.10)
  Reward:risk   2.0
  Charges       about ₹0.84 a share round trip (at 100 shares)
  Net R         1.76 after charges
  Flat by       15:10 IST, win or lose: no overnight hold
```

The 1005.25 midpoint is not a tick, so the stop drops to 1005.20. The 2R target
is 1015.00 + 2 x 9.80 = 1034.60. nse-budget then sizes it: a ₹1,000 max loss
per trade, with charges added to the loss at the stop, would allow 93 shares,
but on the default ₹1,00,000 budget the 75% position cap binds first:
₹75,000 / ₹1,015.00 = 73 shares, ₹788.66 lost at the stop with charges.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk, net R.
  It never prints a P&L or a return.
- Nothing here promises winning trades or money. A 1R target is not a
  prediction that the price gets there.
- Stocks near their price band can lock at the upper or lower circuit, and a
  stop cannot fill in a locked market. nse-risk checks that.
- Long side only. v1 plans longs only.

## What it never does

It never places an order, never sizes a position, never widens a stop to
make a plan pass, and never calls a stop a guarantee.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval".
  Nothing is placed without your yes. Never set an order tool to "Always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
