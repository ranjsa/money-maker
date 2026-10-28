# The NSE Day Trader skill

11 Claude skills for day trading on NSE: scan every EQ chart, track exchange filings and news, time every entry and exit, and replay every outcome before you risk a rupee.

Free, MIT, no signup, no API key. The tools are plain Python that read free
public data: Yahoo Finance charts, NSE's public archives (the EQ symbol list,
price bands, bulk and block deals, announcements, results and insider
filings), NSE's holiday feed and Google News India.

One scans every EQ chart for setups. Another tracks the news and the exchange
filings on every stock you watch. One watches a setup bar by bar and tells you
the moment to get in, and another sets your stop and your target, rounded to
NSE's tick, before you do. And the craziest one is the Time Machine: before
you risk a rupee, it replays the rest of the session 10,000 times from the
stock's own recent bars, plus every past day the same setup fired, and tells
you the honest odds, net of charges.

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Paper trading is the default.

A port of [Jake Schincariol's day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) (MIT) to Indian cash equities.

## Install

Paste this into Claude:

```
https://github.com/tusharjha/nse-day-trader-skill

install skill
```

Or as a plugin, in Claude Code:

```
/plugin marketplace add tusharjha/nse-day-trader-skill
/plugin install nse-day-trader-skill@nse-day-trader-skill
```

Claude Code namespaces plugin skills, so installed as a plugin they show up as
`/nse-day-trader-skill:nse-scanner` and so on. Copy the folders instead if you want
plain `/nse-scanner`:

```bash
git clone https://github.com/tusharjha/nse-day-trader-skill.git
cp -r nse-day-trader-skill/nse-* ~/.claude/skills/
```

Project-local instead of global: copy the same folders into your repo's
`.claude/skills/`. No Claude Code at all? Paste any single `SKILL.md` at the
top of a chat and it runs as a mode. You lose the Python tools, but the method
works.

The tools need Python 3.8 or newer. Nothing to pip install. Your rules live in
`nse/config.json` in your project: start from
`nse-budget/config.example.json` (₹1,00,000 budget, ₹1,000 max loss a trade,
₹3,000 a day, MIS, flat by 15:10 IST, paper), or let `/nse-budget` ask you for
each value.

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/nse-scanner` | Chart Scanner | Scans every NSE EQ chart for setups: opening-range breakouts, flags, oversold bounces. |
| `/nse-news` | News Monitor | Tracks Google News India headlines and NSE announcements on every stock you watch and flags what moves it. |
| `/nse-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. |
| `/nse-exit` | Exit Planner | Sets your stop and your target, tick-rounded, before you enter, so you know when to get out. |
| `/nse-timemachine` | Time Machine | Replays every possible outcome of a trade before you risk a rupee: 10,000 simulated sessions plus every past time the setup fired. |
| `/nse-risk` | Risk Manager | Checks every trade against your limits, the entry window and the price bands, and stops anything over them. |
| `/nse-budget` | Budget Manager | Sizes every position to your budget and your max loss per trade, charges included. |
| `/nse-broker` | Order Executor | Shows each order as a plain ticket, paper trading by default, never without your yes. |
| `/nse-alerts` | Price Alerts | Rings when a price, a stop, the flat-by time or the opening bell hits. |
| `/nse-analyst` | Results Analyst | Reads a company's quarterly results and sums up what changed, in crore. |
| `/nse-deals` | Deals Tracker | Reads NSE's bulk, block and insider-trading disclosures and flags the stocks you watch. |

## How to use it

Everything lands in an `nse/` folder in your project (a different folder from
the upstream US pack, so both can live side by side), and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
  nse/watchlist.json  nse/entry.json  nse/plan.json  nse/timemachine.json
  nse/orders.json  nse/state/risk.json  nse/state/broker.json
alerts: stop, target, flat by 15:10 IST                                       (while it is open)
news / analyst / deals                                                        (on a schedule)
```

An example, the trade in the reel: RELIANCE, Tuesday October 27 2026, real
data. Every number below is what the tools print on the captured bars, and
`tests/test_reel.py` checks each one.

1. **`/nse-scanner`** at 12:40 PM IST: of the 38 usable charts in the reel's
   39-stock scan, 24 had closed above their 15-minute opening range by then.
   RELIANCE is the latest one: range ₹1,189.70 to ₹1,197.00, the 12:35 candle
   closed at ₹1,197.20, 0.02 % over the top.
2. **`/nse-news`**: the morning's headlines and NSE announcements for
   RELIANCE, in IST, each tagged (earnings, board, deal, regulatory...),
   before you decide anything.
3. **`/nse-entry`** watched RELIANCE bar by bar. At 10:15 AM it said waiting:
   nothing had closed above ₹1,197.00. At 12:40 PM it said triggered: the
   12:35 bar (bar 40) closed at ₹1,197.20, the first 5-minute close above the
   range.
4. **`/nse-exit`** set the plan before the entry: stop at the range's
   midpoint, ₹1,193.35 rounded down to the ₹0.10 tick, ₹1,193.30, so ₹3.90 a
   share at risk; target two times that risk, ₹1,205.00. Charges come to about
   ₹0.90 a share round trip, so the 2R target is 1.44 R net. Flat by 15:10 IST
   either way.
5. **`/nse-timemachine`** replayed the rest of the day 10,000 times (30
   five-minute bars to 15:10), each candle drawn from RELIANCE's own candles at
   that time of day over the previous 57 full sessions, 2026-08-03 to
   2026-10-26 (seed 19, so anyone can repeat it): target first 15.5 %, stop
   first 58.7 %, still open at 15:10 25.8 %. Average R -0.156 before charges,
   -0.314 net. It also re-ran the same rule on those 57 real sessions: it
   fired 25 times, 7 hit the target, 11 the stop, 7 neither, +0.167 R before
   charges, -0.013 R net. Its verdict: leans toward the stop. That is the
   point of looking before you risk a rupee.
6. **`/nse-budget`** with the default ₹1,00,000 budget and ₹1,000 max loss:
   the max loss alone would allow 220 shares, but the 75 % position cap binds
   first, ₹75,000 / ₹1,197.20 = 62 shares (₹74,226.40). If the stop fills at
   ₹1,193.30 the loss is 62 x ₹3.90 = ₹241.80 plus ₹73.46 charges, ₹315.26.
   Kite has no bracket orders for retail, so it writes three legs: BUY 62
   RELIANCE at market (MIS), SELL 62 SL-M triggered at ₹1,193.30, SELL 62
   LIMIT at ₹1,205.00.
7. **`/nse-risk`** refreshes NSE's price bands and checks the group against
   your limits: the max loss, the daily loss limit, trades per day, reward to
   risk, the 09:30 to 14:45 entry window, the series (EQ only), the upper
   circuit, position size and cash. PASS: RELIANCE has no fixed price band
   (an F&O stock), ₹315.26 at the stop is inside ₹1,000, and ₹25,773.60 cash
   is left after it.
8. **`/nse-broker`** shows you the three legs in plain words. You type
   `yes 62 RELIANCE` for that exact group. In paper mode (the default) the
   local paper engine plays it on the day's real 5-minute bars: BUY 62 at
   ₹1,197.30, the open of the 12:40 bar; the 13:00 bar's low ₹1,192.80
   reached the stop, SELL 62 at ₹1,193.30. P&L: gross -₹248.00, charges
   ₹73.46, **net -₹321.46**, a little worse than the ₹315.26 plan because the
   entry filled ten paise above the plan's price. The Time Machine said this
   trade leaned toward the stop, and on this day it went there.
9. **`/nse-alerts`** rang at 1:10 PM: RELIANCE ₹1,192.10, at or below your
   stop ₹1,193.30. Its other two alerts were the ₹1,205.00 target and 15:10
   IST, flatten whatever is still open.

## The tools

All standard-library Python. The data tools read public endpoints and are
polite about it.

```bash
python3 nse-scanner/scan.py universe                                # NSE EQUITY_L.csv, series EQ only
python3 nse-scanner/scan.py daily                                   # before the open: setups from daily closes
python3 nse-scanner/scan.py orb --at 12:40                          # opening-range breakouts so far (IST)
python3 nse-news/news.py --tickers RELIANCE --hours 12              # headlines, NSE announcements, today's move
python3 nse-entry/entry.py check RELIANCE --setup orb --write       # waiting / triggered / expired
python3 nse-entry/entry.py watch RELIANCE --every 30                # bar by bar until it triggers
python3 nse-exit/exit.py plan --stop or-mid --target 2R --write     # -> nse/plan.json
python3 nse-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 nse-alerts/alerts.py quote RELIANCE --write                 # -> nse/prices.json (previous close)
python3 nse-budget/size.py --write                                  # -> nse/orders.json, three Kite legs
python3 nse-risk/check.py --refresh-bands                           # PASS / BLOCK per group
python3 nse-broker/ticket.py show                                   # the tickets, READY or not
python3 nse-broker/ticket.py approve --index 1 --reply "yes 62 RELIANCE"
python3 nse-broker/paper.py run --group RELIANCE-2026-10-27-1       # paper fills on 5-minute bars
python3 nse-alerts/alerts.py add --from-plan                        # stop, target, flat by
python3 nse-alerts/alerts.py check                                  # rings what hit, once
python3 nse-alerts/alerts.py calendar --refresh                     # NSE holidays vs NSE's feed
python3 nse-analyst/results.py RELIANCE                             # quarters, growth, EPS, in crore
python3 nse-deals/deals.py today --watch RELIANCE,TCS               # new bulk and block deals
```

Every fetcher takes `--offline DIR` to read saved data instead of the network,
and most take `--now "YYYY-MM-DD HH:MM"` (IST) to replay a past session.

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline, on fixtures cut from real data: NSE's EQ list, the
5-minute bars of October 27 2026 for the scan, RELIANCE's 5-minute bars for
the 60 days to October 27, NSE's price bands, holiday feed, announcements and
deals, and Yahoo's results data. `tests/test_reel.py` runs the whole chain
above in a fresh folder and reproduces every number in the example. It
replays the paper fills with `paper.py run --approved-at`, because a risk
check run on a pinned clock is never READY against the real one: the broker
shows the three legs, marks them RECHECK and refuses the yes, as it should.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
Neither this project nor its author is registered with SEBI as an investment
adviser or research analyst, and nothing here is SEBI-registered research or
advice. You can lose money. Decide for yourself, and talk to a SEBI-registered
adviser if you need advice.

**Most individual traders lose money.** SEBI studies found it, again and
again. SEBI's January 2023 study of FY22 found about 89 % of individual F&O
traders lost money. SEBI's September 2024 study of FY22 to FY24 found about 9
in 10 individual F&O traders (93 %) lost money. SEBI's July 2024 study of
intraday cash-segment trading found about 70 % of individual intraday traders
made losses in FY23. This pack trades cash equity intraday, the last group.

**No trade goes through until you say yes.** Only `/nse-broker` can lead to
an order, and only after `/nse-risk` passes that exact group and you type
yes for it in the chat. Set your broker's order tools to "Needs approval" in
Claude (Customize, Connectors, tool permissions), never "Always allow", so
Claude's own "Allow once / Deny" card is a second check. No F&O, margin, short
selling or leverage unless you turn them on.

**Paper first.** `"paper": true` and `"broker": "paper"` are the defaults in
`config.example.json`. Indian brokers do not offer paper (demo) accounts, so
the local paper engine simulates fills on the stock's real 5-minute bars: the
entry at the next bar's open, the stop at its trigger (or the open, on a gap),
the target at its price. Slippage, partial fills and queue position are not
modelled, so real fills can be worse.

**Going live is your own step.** Zerodha's hosted Kite MCP server
(`https://mcp.kite.trade/mcp`) is read-only for orders: it has no
place, modify or cancel order tools, so it can read quotes, orders and
positions but cannot trade. Live orders need the self-hosted
[zerodha/kite-mcp-server](https://github.com/zerodha/kite-mcp-server), built
yourself with your own Kite Connect API key and secret. Keys live in the
Claude Desktop config or environment variables, never in the chat.

**MIS is squared off for you.** Intraday (MIS) positions still open near the
close are auto squared off by the broker, around 15:20 IST at Zerodha
(brokers differ, and some charge for it). The default `flat_by` is 15:10 so
you close first.

**Circuits.** Most NSE stocks outside F&O have a fixed price band (2, 5, 10
or 20 % from the previous close). A stock locked at its upper circuit cannot be sold.
`/nse-risk` blocks an entry within 3 ticks of the upper band, and any 2 % or
5 % band stock unless you set `"allow_banded": true`. Refresh the bands daily
(`check.py --refresh-bands`).

**Tick sizes.** NSE's tick depends on the price (₹0.01 under ₹250, ₹0.05 to
₹1,000, ₹0.10 to ₹5,000, ₹0.50 to ₹10,000, ₹1 to ₹20,000, ₹5 above; revision
effective April 15 2025, reviewed monthly). Stops round down to the tick,
targets to the nearest tick. Check NSE's current table if a price looks off.

**Charges are estimates.** Brokerage, STT, exchange transaction charges, SEBI
fees, stamp duty and GST are modelled with Zerodha-style defaults that you can
edit under `charges` in your config. Your broker's contract note is the real
number: check it.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 60 days of 5-minute bars. The future can do things those 60
days never did: results, a block deal, a circuit, a headline. Treat its odds
as a reality check, never as a promise.

**A stop is a plan, not a guarantee.** In a fast market or across a gap, an
SL-M order can fill well past its trigger. The max loss shown is the loss at
the stop price, charges included.

**The data is free and unofficial.** Yahoo Finance's chart endpoint is not an
official API: it can lag, change or rate-limit you. NSE's archives and RSS
feeds are public files, not a data service. The tools identify themselves,
wait at least 0.25 s between requests and fetch one thing at a time. The
scanner and the entry timer read 5-minute bars, not every tick. Check
anything that matters against your broker before you act on it.

**Taxes.** Intraday equity profits and losses are generally speculative
business income in India, reported in ITR-3.
Nothing here is tax advice: ask a chartered accountant.

## Files

```
nse-scanner/     SKILL.md, scan.py
nse-news/        SKILL.md, news.py
nse-entry/       SKILL.md, entry.py
nse-exit/        SKILL.md, exit.py
nse-timemachine/ SKILL.md, timemachine.py
nse-risk/        SKILL.md, check.py
nse-budget/      SKILL.md, size.py, config.example.json
nse-broker/      SKILL.md, ticket.py, paper.py, claude_desktop_config.example.json
nse-alerts/      SKILL.md, alerts.py, alerts.example.json
nse-analyst/     SKILL.md, results.py
nse-deals/       SKILL.md, deals.py
tests/           unittest suite + offline fixtures (tests/fixtures/reel.json: the reel's trade)
```

MIT. Made by [Tushar Jha](https://github.com/tusharjha). Forked from [Jake Schincariol's day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) (MIT).
