---
name: nse-scanner
description: >-
  Chart Scanner for NSE. Scans every EQ-series chart on the National Stock
  Exchange for setups: opening-range breakouts, 20-day closing-high breakouts,
  bull flags, oversold bounces. Builds the universe from NSE's EQUITY_L.csv
  (series EQ only, because BE and BZ are trade-to-trade and cannot be day
  traded), runs a pre-market scan of daily closes and an intraday
  opening-range breakout scan on 5-minute bars, then writes
  nse/watchlist.json for nse-news and nse-entry. Free Yahoo data, bar by bar,
  all times in IST. Use when the user says "scan NSE", "Nifty breakouts",
  "what's breaking out today", "find me setups", "opening range breakouts",
  "scan for bull flags", "oversold stocks", "pre-market scan", "build my
  watchlist", "what should I look at today", or at the start of a trading day
  before /nse-news.
argument-hint: "[universe | daily | orb] [--at HH:MM]"
---

# nse-scanner

The Chart Scanner is the first step of a day trade on the NSE. It reads
thousands of free charts and lists the ones that match a rule. That is all it
does. A scan is a list of charts that match a rule, not a list of stocks to
buy, and a setup on a chart is not a prediction. Most people who day trade
lose money (SEBI's studies of individual traders found the majority of
intraday cash traders loss-making); the scanner does not change that. It only
saves you from flipping through 2,400 charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                          # -> nse/universe.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                             # before the open -> nse/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols RELIANCE,TCS,M&M  # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                               # after 09:35 IST -> nse/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15                    # cut the clock at 10:15 AM IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist nse/watchlist.json --json
```

Flags: `--min-price 20` and `--min-turnover-inr 50000000` (Rs 5 crore of
close times volume; daily uses the last session, orb the session bars so
far), `--or-minutes 15`, `--max-confirm 200` (orb stage 2),
`--now "2026-10-27 10:15"` (IST) to pin the clock, `--offline PATH` to read
saved Yahoo files, `--no-write`, `--show N`, `--json`. Exit code 0 when the
scan found something, 1 when it found nothing or could not run (the reason is
one plain line).

## What it reads and writes

| file | what |
| --- | --- |
| `nse/universe.json` | written by `universe`: `{"as_of", "symbols": [...], "names": {SYMBOL: name}}`. nse-news reads the names |
| `nse/watchlist.json` | written by `daily` and `orb`: `created_by`, `date`, `setup` (`orb` or `daily`), `symbols[]` with `symbol`, `yahoo`, `or_high`, `or_low`, `close`, `pct_over`, and `level` (the 20-day closing high on a daily breakout) |
| `nse/config.json` | read by `orb`: `no_entries_after` (default 14:45), `special_sessions` |

nse-news and nse-entry read `symbols[].symbol` (and `yahoo` for the quote).

## The universe

`universe` downloads `EQUITY_L.csv` from nsearchives.nseindia.com and keeps
the rows whose series is `EQ`. The file's header cells carry leading spaces
(` SERIES`, ` DATE OF LISTING`); they are stripped. BE and BZ rows are out:
they are trade-to-trade (delivery only), so there is no intraday to trade.
Symbols keep their `&` and `-` as listed (`M&M`, `BAJAJ-AUTO`, `J&KBANK`); the
Yahoo symbol is the NSE symbol plus `.NS` and is URL-encoded in requests
(`M%26M.NS`).

## The daily scan (before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request, so the 2,367 EQ
symbols (the 2026-10-28 list) take 119 requests at a polite 0.3 s apart. Liquidity: last price at
least `--min-price` (Rs 20) and last session's turnover (close times volume)
at least `--min-turnover-inr` (Rs 5 crore). Run it before the open or after
the close: today's daily bar is left out until 15:30 IST.

The setups are computed from daily closes only:

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

Hits are listed most traded first (close times volume).

## The opening-range breakout scan (during the session)

The NSE normal session runs 09:15 to 15:30 IST. The opening range is the first
`--or-minutes` (15) of it: the 09:15, 09:20 and 09:25 five-minute bars. A hit
is the first completed 5-minute bar to CLOSE above the range high, at that
bar's close time. `--at 09:35` means the bars completed by 09:35 IST, so the
09:30 bar counts.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range. That is a necessary condition
   (the range high is at least every range close), so no breakout is lost.
2. Stage 2, each candidate: the v8 chart with real highs, lows and volume, one
   request each. Range high = the highest high of the range bars, range low =
   the lowest low. On a strong morning most charts are candidates, so stage 2
   checks at most `--max-confirm` (200) of them, the most traded first, and
   prints PARTIAL with the count it skipped. `--max-confirm 0` checks them all.

A chart is usable only when every 5-minute bar from 09:15 to the cutoff is
there; a missing bar means a halt or a thin stock. Null closes are dropped, a
bar off the 5-minute grid (the live tick) is dropped, and a bar counts only
once it is complete (start + 5 minutes).

It prints one plain line and writes nothing before 09:35 IST ("opening range
not complete until 09:30 IST"), after the close, after `no_entries_after`
(an expired setup), and on a weekend or holiday ("market closed:
Diwali-Balipratipada"), without fetching anything. The 2026 NSE holiday list
is built in; a date outside 2026 is treated as Mon-Fri with the warning "NSE
calendar for <year> not loaded". A Muhurat or other special session is a
`special_sessions` entry in `nse/config.json`.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag. Bars are
  5-minute bars, bar by bar, never tick by tick. query2 first, query1 on a 429
  or 5xx, one retry after a 5-second wait on a 429.
- User-Agent `Mozilla/5.0 (nse-day-trader-skill)`, at least 0.25 s between
  requests. Yahoo keeps only about 60 days of 5-minute history; older days
  need `--offline`.
- Partial results are said as partial: "Fetched 1,840 of 2,010 charts (170
  failed or had no data)". Never round a partial scan up to "the whole market".
- A stock at its price band (circuit) limit can lock up; a breakout there may
  not be exitable. nse-risk checks this, the scanner does not.

## Steps

1. Once a week or so: `scan.py universe`.
2. Before the open: `scan.py daily`. Tell the user the counts (fetched,
   liquid, scanned, hits) and the top few by setup.
3. After 09:35 IST (10:15 AM IST is the classic): `scan.py orb`. Tell the
   user how many usable charts broke their range and when, and that a later
   hit is closer to the price now.
4. The user picks one chart. Hand it on: `/nse-news SYMBOL` (what is moving
   it), then `/nse-entry SYMBOL` (the entry bar by bar). Never pick for them.

## Example: a replay

Tuesday 2026-10-27, scan at 10:15 AM IST over 38 real EQ charts (trimmed
Nifty names, `tests/fixtures/nse-scanner/chart-5m-2026-10-27.json.gz`):

```
python3 scan.py orb --offline chart-5m-2026-10-27.json.gz --now "2026-10-27 10:15"
```

20 of the 38 closed above their opening-range high by 10:15 AM IST. The
synthetic `DEMO` fixture is the textbook case: range high Rs 1,012.40, low
Rs 998.10, and the 09:30 bar closes at Rs 1,015.00, which is +0.26 % over, so
the hit reads 09:35 IST. The user picks one name and Claude hands it to
`/nse-news SYMBOL`, then `/nse-entry SYMBOL`.

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data.
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval".
  Nothing is placed without your yes. Never set an order tool to "Always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
