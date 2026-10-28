---
name: nse-budget
description: >-
  Budget Manager for NSE. Sizes every intraday position to your budget and
  your max loss per trade, with brokerage, STT, GST and the other charges
  counted inside the loss at the stop. Reads the trade plan from nse-exit
  (entry, stop, target), finds the whole shares that fit inside four limits
  (max loss per trade, position cap, max order, cash after your buffer), says
  which limit decided it, and writes ONE three-leg MIS order (entry, SL-M
  stop, LIMIT target) to nse/orders.json in Kite Connect place_order field
  names. A stop too far away for your max loss is a plain refusal, not a
  smaller order. Nothing is placed. Use when the user says "how many shares",
  "size this trade", "how much should I buy", "I have 1 lakh", "I only want
  to risk 1000 a trade", "set my rules", "keep 10% in cash", or after
  /nse-exit writes a plan.
argument-hint: "[budget in rupees]"
---

# nse-budget

The Budget Manager turns a plan ("buy DEMO at ₹1,015.00, stop ₹1,005.20")
into an exact order ("buy 73 shares"), inside rules the user sets once. The
stop decides the size: the further away the stop, the fewer shares. It writes
a proposal only. nse-risk checks it and nse-broker asks the user yes or no.
Intraday trading loses most people money (SEBI's studies say so, see the
README). Sizing to a max loss keeps each loss small; it does not make a
trade win. Not financial advice.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                         # nse/plan.json + nse/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                 # write nse/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit --write         # a LIMIT entry at the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1014.50 --json  # a LIMIT entry at ₹1,014.50
```

`--now "2026-10-27 10:15"` pins the clock (IST). A plan for another day is
refused: make a new one with nse-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `nse/config.json` | the rules (start from `config.example.json` in this folder) |
| `nse/plan.json` | the trade: entry, stop, target, setup, flat-by time (nse-exit writes it) |
| `nse/prices.json` | optional: a fresh price above the plan's entry means a bigger risk (nse-alerts writes it) |
| `nse/portfolio.json` | optional: its cash caps the size (nse-broker keeps it) |
| `nse/orders.json` | the one proposed order it writes with `--write` |

## Step 1: set the rules once

If `nse/config.json` does not exist, copy `config.example.json` into
`nse/config.json` and ask the user for each value in plain words:

- **budget**: how much money this is for (default ₹1,00,000). Only money they can afford to lose.
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **max_loss_per_trade_inr**: the most one trade may lose if the stop fills at the stop,
  charges included (default ₹1,000).
- **max_position_pct**, **max_order_inr**, **min_order_inr**: the biggest position (75% of the
  budget), the biggest order (₹80,000) and the smallest order (₹500).
- The day limits nse-risk enforces: **max_daily_loss_inr** (₹3,000), **max_trades_per_day** (3),
  **min_reward_risk** (1.0), **require_stop** (true), **no_entries_before** / **no_entries_after**
  ("09:30" / "14:45") and **flat_by** ("15:10"), all IST.
- **product** "MIS" and **exchange** "NSE": intraday cash equity, EQ series only.
- **charges**: optional overrides for the fee schedule (brokerage, STT, GST...). Empty uses the defaults.
- **blocked**: symbols they never want to trade.
- Keep **paper** true (and **broker** "paper") until they have watched it work.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only, never fractional. The size is the smallest of four limits:

| limit | shares |
| --- | --- |
| max loss | the most shares where shares x risk + round-trip charges at the stop stays within `max_loss_per_trade_inr` |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order_inr` / entry |
| cash | (budget minus the cash buffer) / entry, never more than the cash in `nse/portfolio.json` |

Charges matter on a tight stop: brokerage, STT on the sell side, exchange and
SEBI fees, stamp duty and 18% GST are added to the loss at the stop, so the
loss you cap is the loss you would really take. The script starts at
`floor(max loss / risk)` and takes shares off until the net loss fits.

The entry it sizes at: the limit price with `--limit`. For a market entry, the
plan's entry, or the price in `nse/prices.json` if it is fresh (under 15
minutes old) and higher. A price that ran up since the plan means a bigger
loss at the same stop, so fewer shares. A lower price never shrinks the risk.

It refuses, and writes no order, when: the size is zero shares (the refusal
reads "a stop ₹X away needs max loss ≥ ₹Y"), the order is under the minimum,
reward:risk is under `min_reward_risk`, the stop is not below the entry, the
target is not above it (the move already happened), the plan is for another
day, the symbol is blocked, or the plan is a short. Say the reason to the
user in one line. Never offer a smaller order or a wider limit to make it fit.

## Step 3: the three-leg order

Kite has no bracket orders for retail accounts, so the proposal is three
separate Kite Connect `place_order` legs sharing one `group` and one quantity:

| role | transaction_type | order_type | price field |
| --- | --- | --- | --- |
| entry | BUY | MARKET (or LIMIT with `--limit`, plus `price`) | none / `price` |
| stop | SELL | SL-M | `trigger_price` |
| target | SELL | LIMIT | `price` |

All legs are `product` MIS, `variety` regular, `validity` DAY. The stop and
target are exits for the same shares: when one fills, the other must be
cancelled, or it could open an unwanted short. nse-broker prints that cancel
ticket and nse-alerts rings if both legs are still open after a fill. The
`group` is `SYMBOL-DATE-n`, and `n` counts the groups already in the file.

## Step 4: show the user

Show the four limits, which one decided the size, the shares, the rupees and
the max loss at the stop with the charges shown separately. Say plainly that
a stop is an order, not a promise: in a fast market it can fill below the
stop. Then hand over to `/nse-risk`.

## Example

DEMO on Tuesday 2026-10-27. Opening-range breakout: entry ₹1,015.00 at
09:35 AM, stop ₹1,005.20 (the opening-range midpoint), target ₹1,034.60 (2R).
Risk ₹9.80 a share. The example config: ₹1,00,000 budget, 10% buffer, ₹1,000
max loss.

| limit | the math | shares |
| --- | --- | --- |
| max loss | ₹1,000 less charges, ₹9.80 a share | 93 |
| position cap | 75% of ₹1,00,000 = ₹75,000 / ₹1,015.00 | **73 (decides it)** |
| max order | ₹80,000 / ₹1,015.00 | 78 |
| cash | ₹1,00,000 minus ₹10,000 = ₹90,000 / ₹1,015.00 | 88 |

73 shares = ₹74,095.00. Max loss at the stop: 73 x ₹9.80 = ₹715.40 plus
₹73.26 charges = ₹788.66. It writes (entry leg shown in full):

```json
{"group": "DEMO-2026-10-27-1",
 "legs": [{"role": "entry", "exchange": "NSE", "tradingsymbol": "DEMO", "transaction_type": "BUY",
           "quantity": 73, "product": "MIS", "order_type": "MARKET", "variety": "regular", "validity": "DAY"},
          {"role": "stop", "...": "SELL 73, SL-M, trigger_price 1005.2"},
          {"role": "target", "...": "SELL 73, LIMIT, price 1034.6"}],
 "why": "...", "plan": {"entry": 1015.0, "stop": 1005.2, "target": 1034.6, "charges": 73.26, "max_loss": 788.66}}
```

`why` and `plan` are notes for nse-risk and nse-broker. They are never sent
to the broker. With a stop ₹1,200 away instead, no order is written. If the
cap is lifted to 100% with no buffer, the max loss limit decides and the size
is the 93 shares above.

## Rules

- Proposals only. This skill never calls a broker tool.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Free Yahoo prices are unofficial and can lag. The broker's price is the one that fills.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, and never sizes F&O, leverage
or short positions.

## Safety

- Education and tooling. Not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when nse-risk passed that exact
  order today and the user typed an explicit yes for that one order in the chat. Without a typed yes,
  nothing is placed.
- No F&O, leverage or short selling unless the config allows it and the user confirms.
- Broker keys live in environment variables, never in the chat.
