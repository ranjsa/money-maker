---
name: nse-deals
description: >-
  Deals Tracker for NSE. Watches who trades big and who inside a company
  trades its own shares: bulk deals (over 0.5% of a company's equity in a
  day), block deals (window trades of at least 10 crore) and SEBI insider
  trading (PIT) disclosures by promoters, directors and key managerial
  persons. Shows only what is new since the last check, newest first, with
  the client, side, quantity, price and value in crore, flags symbols on your
  watchlist or in your portfolio, and keeps its own history so you can ask
  what a given client has been doing. Public filings, not tips. Use when the
  user says "any bulk deals today", "block deals", "who bought", "what is
  the promoter doing", "insider buying", "insider trading disclosures",
  "what has this fund been buying", "big deals on my watchlist", or "run the
  deals skill".
argument-hint: "[today | insider | client NAME] [--watch SYMBOLS]"
---

# nse-deals

NSE publishes two lists of unusually large trades every trading day, and SEBI
makes insiders disclose their own trades. This skill reads all three, tells
you only what is new, and remembers it, because the exchange files hold the
current day only and are overwritten the next morning.

These are public filings read quickly. There is no inside information here,
and a big buyer is not a signal that a stock will rise. Say that every time.
Not financial advice.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/deals.py" today
python3 "${CLAUDE_SKILL_DIR}/deals.py" today --watch STLTECH,TCS --min-value-cr 5
python3 "${CLAUDE_SKILL_DIR}/deals.py" insider
python3 "${CLAUDE_SKILL_DIR}/deals.py" client "serum institute"
```

## What the three feeds are

- **Bulk deals**: any client whose trades in one stock in one day add up to
  more than 0.5% of the company's listed equity. The list names the client and
  the weighted average price. It comes out after the close.
- **Block deals**: single trades of at least 10 crore (and a minimum size)
  done in NSE's separate block windows (a morning window and an afternoon
  window), at or close to the market price. Both buyer and seller are named.
- **Insider trading (SEBI PIT)**: promoters, directors, key managerial
  persons (KMP), designated persons and their relatives must disclose a trade
  above 10 lakh in a quarter within two trading days. Each filing is an XBRL
  file listing person, category, buy or sell, quantity, value, dates and mode
  (market purchase, ESOP, pledge, off-market...).

## What it reads and writes

| file / feed | what |
| --- | --- |
| `https://archives.nseindia.com/content/equities/bulk.csv` | bulk deals, current day only |
| `https://archives.nseindia.com/content/equities/block.csv` | block deals, current day only |
| `https://nsearchives.nseindia.com/content/RSS/Insider_Trading.xml` | the latest PIT filings, each linking an XBRL file |
| `nse/watchlist.json`, `nse/portfolio.json` | symbols to flag with a star (`--watch SYM,...` limits the output to symbols; a bare `--watch` limits to these files) |
| `nse/state/deals.json` | what has been shown (each row alerts once) and the full history of every row ever fetched |

Requests are paced at 0.25 s or more with the user agent
`Mozilla/5.0 (nse-day-trader-skill)`. `--offline DIR` reads saved copies of
the files instead of the network.

## The rules

- **Public filings, not tips.** Bulk and block lists are published after the
  trade, and PIT filings arrive up to two trading days after it. The move may
  already have happened. Never present a deal as a reason to buy.
- **Both sides appear.** Bulk deals often list the same client on the BUY and
  the SELL (intraday traders, arbitrage desks, market makers). A BUY and a
  SELL by the same client on one day is churn, not conviction. Say so.
- **Value is quantity times price**, exactly, shown in crore. A block deal
  buyer and seller at one price usually means a negotiated transfer.
- **Insider is not only buying.** Read the `side` and `mode`: ESOP allotment
  is pay, a pledge is borrowing against shares, and neither is a market buy.
  A market purchase by a promoter or director is the interesting row.
- **The insider feed can lag.** When the newest item is more than 7 days old
  the tool prints "NSE's insider-trading feed looks stale (latest <date>);
  check nseindia.com > Corporate Filings > Insider Trading". On 2026-10-28
  the newest item in the RSS feed was dated 2026-05-02. Say this out loud and
  use the exchange site for anything recent.
- **Market closed** means no new bulk or block file: the tool prints
  "no deals: market closed (<reason>)" on weekends and NSE holidays.
- **History starts when you start.** `client` can only find deals that this
  tool saw on earlier days. Run `today` after the close every trading day (a
  scheduled task or `/loop` is fine) to build it.

## Steps

1. After the close (bulk and block files are published after 15:30 IST), run
   `python3 "${CLAUDE_SKILL_DIR}/deals.py" today`. Use `--min-value-cr 5` on
   busy days and `--watch` to focus on your symbols.
2. Read it: starred rows first (your watchlist and holdings), then the
   largest values. Group a stock's rows together and say who bought, who
   sold, at what price, against the day's close.
3. Run `deals.py insider` once a day. For each row say the person, the
   category, what they did and the mode, and flag pledges and ESOPs as such.
4. If the user asks about a fund or a trader, run `deals.py client "name"`
   and report what the history holds, and that it only goes back to the first
   run.
5. Hand any idea to `/nse-news` (what else happened), then
   `/nse-entry`. A deal alone is never a trade plan.

## Gotchas

- Names are as NSE prints them (`VAIBHAV RAJENDRA DOSHI`, `... PRIVATE
  LIMITED`); the same fund can appear under several entities.
- Exit code 0 even when nothing is new; the text says "nothing new".
- `--dry-run` shows rows without marking them as seen; `--json` gives the
  rows with `value_inr` as strings.
- A file that failed to download is reported on stderr and skipped, so
  "nothing new" after an error can mean the fetch failed: check stderr.

## Safety

Not financial advice. This skill only reads public data and never places an
order. Any trade that follows goes through the order path (budget, risk,
broker), and nothing is sent to a broker unless the user types yes to the
exact ticket.
