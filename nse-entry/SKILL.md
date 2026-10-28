---
name: nse-entry
description: >-
  Entry Timer for NSE. Watches a setup bar by bar and tells you the moment to
  get in. Reads free Yahoo 1-minute or 5-minute bars for an NSE stock
  (RELIANCE.NS style symbols), builds the opening range (09:15 to 09:30 IST by
  default) and calls the entry on the first completed bar that CLOSES above
  the range high, or above a level you give it (like a 20-day high from
  nse-scanner), with an optional volume check. Says waiting and how far,
  triggered with the time and the price in IST, expired, closed (weekend or
  NSE holiday, named) or not open yet, and writes nse/entry.json for
  /nse-exit. Long side only. It never places an order. Use when the user says
  "tell me when to get in", "watch RELIANCE", "has it broken out", "did it
  break the opening range", "entry signal", "time my entry", "check my
  watchlist", or after /nse-scanner and /nse-news, before /nse-exit.
argument-hint: "[SYMBOL] [--setup orb|level] [--interval 5m|1m]"
---

# nse-entry

The Entry Timer turns a setup into one moment: the bar that confirms it. It
reads bars, 1-minute or 5-minute, and waits for each one to finish. A price
that pokes above the high in the middle of a bar is not an entry. A bar that
CLOSES above it is. Then it says the time and the price, writes them down for
the Exit Planner, and stops. It never buys anything. Day trading loses most
people money, and a clean signal does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
Every time is IST (UTC+05:30, no daylight saving).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE                 # 15-minute opening range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --write         # also save nse/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check INFY --setup level --level 1620.50
python3 "${CLAUDE_SKILL_DIR}/entry.py" check "M&M" --interval 1m --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch RELIANCE --until 14:45   # one line per closed bar, stops on the trigger
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                      # every symbol in nse/watchlist.json
```

Other flags: `--or-minutes N` (default 15), `--json`, `--offline FILE` (a
saved Yahoo chart JSON, `.json` or `.json.gz`, or a folder of
`<SYMBOL>.json`), `--now "2026-10-27 10:15"` (IST) to replay a day.
`watch` with `--now` or `--offline` runs on a simulated clock and replays the
day bar by bar without waiting. Symbols are NSE symbols; Yahoo gets
`SYMBOL.NS`, with `&` encoded (`M&M` becomes `M%26M.NS`).

## What it reads and writes

| file | what |
| --- | --- |
| `nse/watchlist.json` | the scanner's hits (`symbols[].symbol`), for `watchlist` |
| `nse/config.json` | `no_entries_before` (09:30), `no_entries_after` (14:45), IST, and `special_sessions` for a Muhurat evening. Optional |
| Yahoo chart API | today's bars, `range=1d&interval=5m` (or `1m`): free, unofficial, can lag |
| `nse/entry.json` | with `check --write`, or when `watch` triggers: symbol, setup, status, time, bar, price, level, opening range, `"exchange": "NSE"`, `"source": "yahoo"` |

## The rule

- **Opening range (orb).** The high and low of the first 15 minutes of the
  09:15 open: the 09:15, 09:20 and 09:25 five-minute bars, or fifteen 1-minute
  bars. The level to beat is the range high.
- **Level.** A price you give, like a 20-day high from `/nse-scanner`.
- **The trigger** is the first completed bar after the range that CLOSES
  above the level. A wick above is not a trigger. A close exactly at the
  level is not above it.
- **The time** is that bar's close time (the 09:30 bar closes at 09:35 AM).
  The price is its close, to the paisa.
- **Only closed bars count.** A bar is closed when the clock passes its start
  plus 5 (or 1) minutes and Yahoo has caught up to that moment (it waits a
  minute at most). Yahoo's live tick, like 10:49:45, is never a bar.
- **Volume, if you want it.** `--min-volume-x 1.5` also needs the trigger
  bar's volume at 1.5 times the range bars' average. The 09:15 bar holds the
  opening auction, so 1.0 is already strict.
- **Time limits.** No entries before 09:30 or after 14:45 (config), well
  ahead of the broker's intraday square-off. NSE has no early closes, so the
  cutoff never moves.
- **What it says.** Waiting (and how far the last close is from the level),
  triggered, expired, or no data. Before 09:15: not open yet. Before 09:30:
  the range is still forming. Weekends and NSE holidays: closed, with the
  holiday's name, for example Diwali-Balipratipada on 2026-11-10.
- **Calendar.** The holiday list covers 2026. For another year the tool says
  "NSE calendar for 2027 not loaded", points at
  `nse-alerts/alerts.py calendar --refresh` or nseindia.com, and treats
  Mon-Fri as open. Say so to the user instead of trusting a "waiting".

## Steps

1. Get the symbol from the user, or run `entry.py watchlist` after
   `/nse-scanner`. Check `/nse-news` for it first.
2. Run `entry.py check SYMBOL`. Tell the user the status in one line:
   waiting and how far, triggered at what time and price, expired, or closed.
3. To wait for it, Claude can run `entry.py watch SYMBOL --until 14:45` in
   the background, or `entry.py check SYMBOL --write` on a `/loop` every 5
   minutes. `watch` wakes just after each bar closes, prints one line per
   bar, and exits 0 on the trigger (it writes `nse/entry.json`) or 1 when
   the setup expires. Keep the computer awake while it watches: a sleeping
   laptop pauses it, and it catches up, late, when it wakes.
4. When it triggers, say the time and the price, and that nothing was placed.
   If the trigger is more than a bar old, say that too: a late entry is a
   different trade.
5. Hand off to `/nse-exit` to set the stop and the target BEFORE anything
   is bought.

## Example: a synthetic day

DEMO, Tuesday 2026-10-27, 5-minute bars (hand-built test data, not a real
stock). The opening range is 998.10 to 1,012.40. At 09:33 the 09:30 bar is
still open, so it waits:

```
WAITING  last close ₹1,006.00 (09:25 bar), ₹6.40 under the range high ₹1,012.40 (-0.63%).
The 09:30 bar is still forming (₹1,015.00 so far, closes 09:35 AM); it does not count until it closes.
```

At 09:35 the bar has closed:

```
DEMO  Tue 2026-10-27 09:35 IST  opening-range breakout (15-minute range) on 5-minute bars (Yahoo, free, can lag)
Opening range 09:15-09:30: high ₹1,012.40, low ₹998.10 (3 of 3 bars).
TRIGGERED 09:35 AM at ₹1,015.00: the 09:30 bar (bar 3) closed above the range high ₹1,012.40 (+0.26%).
This is the moment to get in, if you take the trade. Nothing was placed.
Next: set the stop and target BEFORE you buy: /nse-exit (exit.py plan --from nse/entry.json).
```

Before the open (09:05) it says `NOT OPEN YET  NSE opens 09:15 AM IST`. On
2026-11-10 it says `MARKET CLOSED  NSE is closed today (Diwali-Balipratipada)`
and names the next open.

## Rules

- Bar by bar, on free Yahoo 1-minute or 5-minute bars. They are unofficial
  and can lag. Say so when a minute matters.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money.
- Long side only. v1 plans longs only. If the user wants a short, say that.
- Only EQ-series stocks can be day traded (BE and BZ are trade-to-trade).
  Near an upper circuit a breakout can lock and be impossible to exit:
  `/nse-risk` checks that, not this tool.
- One trigger per setup per day: the first close above. Later closes do not
  reset it.
- Never call an entry early because a bar looks like it will close above.

## What it never does

It never places an order, never sizes a position, never sets the stop (that
is `/nse-exit`), never moves the level after the fact, and never counts a
bar that has not closed. It never runs `/nse-broker` from a background
watch: nobody is there to say yes.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval".
  Nothing is placed without your yes. Never set an order tool to "Always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
