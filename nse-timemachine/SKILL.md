---
name: nse-timemachine
description: >-
  Time Machine for NSE. Replays every possible outcome of a trade before you
  risk a rupee: 10,000 simulated sessions plus every past time the setup
  fired. Takes the plan from nse-exit (entry, stop, target, flat-by), rebuilds
  the rest of the day 10,000 times from the stock's own 5-minute bars over the
  last 60 days (09:15 to 15:30 IST), and re-runs the same rule on every past
  session. Prints the odds of target first, stop first, or neither by your
  flat-by time, where the price sits then, the average R before and after
  charges, the history, and one honest verdict, then hands off to /nse-budget.
  Use when the user says "run the time machine", "replay this trade", "what
  are the odds", "simulate this trade", "monte carlo", "backtest this setup",
  "how often does this hit the target", "is this a coin flip", or after
  /nse-exit writes a plan, before /nse-budget.
argument-hint: "[path to plan.json, or SYMBOL]"
---

# nse-timemachine

The Time Machine sits between the plan and the money. nse-exit has set the
entry, the stop and the target. Before anything is sized or sent, this skill
asks one question: if today plays out like the stock's own last 60 days, how
often does this plan hit the target first, the stop first, or neither? It
answers with 10,000 replays and with the real record of the same rule on every
past session, in rupees and net of charges. It is a simulation built from the
stock's own recent bars, not a prediction. Day trading loses most people
money, and a good-looking replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # nse/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save nse/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 14:30      # end each replay earlier than the plan's flat-by
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol RELIANCE --date 2026-10-27 \
    --entry 1197.20 --stop 1193.30 --target 1205.00 --entry-time "12:40 PM"
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--config PATH` (default `nse/config.json`), `--offline FILE` (a saved
Yahoo chart JSON, `.json` or `.json.gz`), `--now "2026-10-27 15:30"` (IST) to
pin the clock.

## What it reads and writes

| file | what |
| --- | --- |
| `nse/plan.json` | the plan from nse-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range, flat_by, est_charges_per_share |
| `nse/config.json` | optional: `flat_by` (15:10) when the plan has none, `charges` overrides for the charges model, `special_sessions` |
| Yahoo chart API | one request: 60 days of 5-minute bars (`range=60d&interval=5m`) for `SYMBOL.NS`, free and unofficial, can lag |
| `nse/timemachine.json` | with `--write`: `{plan, mc, history}` plus the verdict. `mc` holds the odds, percentiles, `avg_R` (before charges), `avg_r_net` (after), the fan-chart bands, 160 sample paths and a 24-bin histogram of the price at flat-by |

## How it works

1. **The session.** A trading day is 75 five-minute slots, 09:15 to 15:30 IST.
   Slot 0 is the 09:15 bar and slot 71 is the 15:10 bar. Only FULL sessions
   before the plan's date go in the pool: bars from 09:15 through the 15:10
   bar with no gap. Yahoo's NSE feed drops the closing-auction bars (15:20 and
   15:25 nearly always), so those slots are used where they exist and are not
   required. The Muhurat session, any other special session, a half-empty day
   and today are left out of the pool and the history, and the run says how
   many and which. Every bar becomes four ratios to the bar before it.
2. **One replay.** Start at the entry price. For each bar left, pick a past bar
   from the same time of day (plus or minus 2 bars) at random and scale it to
   the current price. Bar by bar, it checks the stop first, then the target. A
   bar that touches both counts as the stop.
3. **Flat-by.** The replay ends at the plan's `flat_by` (default 15:10 IST). The
   last bar replayed is the one that starts before it (the 15:05 bar) and the
   trade is marked at its close, 15:10. The 15:10 bar itself is not replayed.
4. **10,000 replays** give four odds. Target first. Stop first. Neither, which
   means the trade was still open at flat-by. Green, which means target first or
   above the entry at flat-by. R is +2 at a 2R target, -1 at the stop, and
   otherwise the distance from the entry at flat-by divided by the risk. A gap
   through the stop still counts as -1R here. A real fill can be worse, and a
   stock locked at a circuit limit cannot be sold at all.
5. **Net R.** Charges are real money on a short stop. Each outcome is also
   scored net of charges: `(exit - entry - c) / (entry - stop + c)`, where c is
   the plan's `est_charges_per_share` (round trip, brokerage, STT, exchange and
   SEBI fees, stamp duty and GST) or, when the plan has none, the charges
   model's round trip for 100 shares divided by 100. Gross average R is
   `avg_R`, net is `avg_r_net`. Net is always lower when charges are above zero.
   Print both.
6. **The history.** The plan's own rule (the same opening range, stop rule and
   target R, stops and targets on the NSE tick) runs again on every one of
   those past sessions. That is what really happened, not a simulation.
7. **The verdict** is one line from the numbers. It looks only at the replays
   that hit one side, and asks how often the target came first. A bigger target
   needs to win less often to break even: 1 in (1 + R). That is 50 % at 1R,
   33 % at 2R, 25 % at 3R. Less than 5 points from break-even: "Close to a
   coin flip" at 1R, "Close to break-even" at any other R. 5 to 15 points:
   "Leans toward the target" or "Leans toward the stop". 15 or more:
   "Strongly toward" one of them.
8. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither, the average R when its sign goes against the lean, and
   a line when charges turn a positive average R negative.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one plain
line, when there are fewer than 20 full sessions, when the entry bar is not in
the data (wrong date, not closed yet, a future day), when the plan's day is a
weekend or an NSE holiday (it names the holiday) or a special session, when
`--until` runs past the closing bars Yahoo leaves out, or when the plan does
not make sense (a stop above a buy's entry, an entry off the 5-minute grid).
A live in-progress bar is never used. For a year the holiday list does not
hold it warns and treats Monday to Friday as open.

## Steps

1. Make sure `nse/plan.json` is today's plan from `/nse-exit`. If there is
   none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. The plan's flat-by (15:10 by default)
   already decides what "neither" means.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at flat-by, both average R figures, the history line,
   then the verdict line and any line under it exactly as printed. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do not
   dress it up. If the history and the replays disagree, say that too. If net R
   is negative while gross R is positive, say that charges eat the edge.
5. Say once that this is built from the last 60 days and is not a prediction.
   Results, news, circuit limits and halts can do things those 60 days never
   did. Check `/nse-news` for the stock before going on.
6. If the user still wants the trade, hand off to `/nse-budget` to size it.

## Example: RELIANCE on 2026-10-27

RELIANCE.NS, Tuesday 2026-10-27. The 15-minute opening range is ₹1,189.70 to
₹1,197.00. The 12:35 bar closes above it: entry ₹1,197.20 at 12:40 PM IST (bar
40), stop ₹1,193.30 (the range midpoint, rounded down to the ₹0.10 tick),
target ₹1,205.00 (2R, risk ₹3.90 a share, about ₹0.90 a share in charges).

```
10,000 replays of the rest of the session, 12:40 PM IST to 03:10 PM IST (30 five-minute bars), seed 19.
  target first   15.5 %
  stop first     58.7 %
  neither        25.8 %   still open at 03:10 PM IST
  green          34.0 %   target first, or above the entry at 03:10 PM IST
  Price at 03:10 PM IST: median ₹1,196.38, 90 % of replays between ₹1,185.90 and ₹1,206.96.
  Average R: -0.156 before charges, -0.314 net of charges
History: the same rule on each of those 57 real sessions fired 25 times:
7 target, 11 stop, 7 neither. Average R +0.167 before charges, -0.013 net.
Leans toward the stop: at 2R the target has to come first in 33 % of the replays that hit
one side; it did in 21 % (target first 15.5 % vs stop first 58.7 %).
```

At 2R the target has to come first in a third of the replays that hit one
side. It did in 21 %. The replays lean toward the stop, and the real history
turns slightly negative once charges are paid. That is the honest answer for
this late entry, and it is what the user should hear before `/nse-budget`
sizes any shares.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat them,
  do not rewrite them. Never judge a 2R or 3R plan by target first vs stop
  first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag. Bars are 5-minute bars, checked
  bar by bar, never tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.
- All times are IST, all prices are in rupees.

## What it never does

It never places an order, never sizes a position, never changes the plan, and
never calls a result a sure thing. It never prints a P&L or a profit. It never
uses a bar that has not closed yet, and never mixes today's bars into the pool.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval".
  Nothing is placed without your yes. Never set an order tool to "Always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
