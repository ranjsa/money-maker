---
name: nse-news
description: >-
  News Monitor for NSE. Tracks breaking news on every stock you watch and
  flags what moves it: Google News India headlines per company, NSE corporate
  announcements (results, board meetings, acquisitions, resignations, credit
  ratings, analyst meets) matched to your symbols, and how far each stock is
  from the previous close. Shows only what is new since the last check, newest
  first, in IST, tags each item (earnings, board, deal, management, rating,
  analyst, regulatory, macro...) and never repeats one. Use when the user
  says "any news on RELIANCE", "why is this moving", "what's the news on my
  watchlist", "morning briefing", "pre-market news", "monitor the news", "did
  anything break", "check the announcements", or after /nse-scanner finds
  setups.
argument-hint: "[symbols]"
---

# nse-news

A chart can look perfect and still be about to gap on a results date, a board
meeting outcome or a SEBI order. Before a setup becomes a trade, this skill
reads the two feeds that carry those first, for every stock on the watchlist,
and tells you only what is new. It also prints how far each stock has moved
today, so a headline and a move sit side by side.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                         # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE TCS --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-filings --json
```

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `nse/watchlist.json` | the scanner's symbols (nse-scanner) |
| `nse/plan.json` | the trade you are planning (nse-exit) |
| `nse/portfolio.json` | what you hold (nse-broker records fills there) |
| `nse/universe.json` | `SYMBOL -> Company Name Limited` (`scan.py universe` in nse-scanner); run it once if names are missing |
| Google News India RSS | `https://news.google.com/rss/search?q="Reliance Industries" share&hl=en-IN&gl=IN&ceid=IN:en`, one request per company |
| NSE announcements RSS | `https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml`, one request; each item names the company and its `SUBJECT` |
| Yahoo Finance chart endpoint | the last price against the previous close (unofficial, can lag) |
| `nse/state/news.json` | what has been shown (kept 30 days) |

Announcements are matched to your symbols by exact company name after
normalising (lowercase, no trailing Limited or Ltd, no punctuation, `&` as
`and`). There is no fuzzy matching, so a stock with an unusual name in the
feed can be missed: say so if a result is expected and absent. Duplicate
headlines from different outlets collapse into one. Times are IST: NSE's own
times are IST, Google's GMT times are converted.

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story or the announcement PDF and read it. Quote the
  source and link it. Never summarise a paywalled story you could not read:
  say so.
- **Announcements beat headlines.** If an NSE announcement and a headline
  disagree, the announcement is what the company told the exchange.
- **Say what it touches.** Every item lists its symbol and tags: earnings,
  board, deal, management, rating, analyst, legal, capital, risk, halt,
  regulatory, macro, or `filing` for any other announcement. Announcements
  marked `!` are results, deals, management changes or rating actions.
- **MOVING** marks a stock 3 % or more from the previous close (`--move-pct`).
  A move with no headline is worth saying out loud too.
- **No predictions.** Report what was published and how far the price is from
  the previous close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new. `--all` shows items already seen, `--json` gives everything
   machine readable, `--dry-run` leaves the state alone.
2. Read the items that matter: every `!` announcement, anything tagged
   earnings, board, halt, legal, regulatory or risk, and every MOVING symbol
   first.
3. Write a short brief, per symbol: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "Q2 results
   are this week: an overnight gap can jump past any stop").
4. If a story changes the picture, say so before `/nse-entry` and
   `/nse-exit`: a results date inside the trade, a board meeting, an ASM or
   circuit notice. Never act on a headline alone.

During the session: `/loop 15m /nse-news`, or a scheduled task each morning
before the 09:15 IST open.

## Example

Wednesday 2026-10-28, RELIANCE closed at 1,158.00, 3.19 % below the previous
close of 1,196.20 (`MOVING`). The Google feed that day carried "Reliance
Industries Shares Fall 2% After Jio Platforms Listing; Q2 Results on October
30" (India Infoline, tagged earnings). The brief: "Reliance reports on 30
October, so the stock can gap either way; today's trade is in and out before
the 15:10 IST flat-by time. No new NSE announcement for RELIANCE." For UCO
Bank the NSE feed showed an investor meet notice at 21:01 IST (tag analyst).
When a symbol has no news, say so rather than padding the brief.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline.
- Never hammers a server: one request per feed per company, at least 0.25
  seconds between calls, and `--offline DIR` to read saved files instead.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. It is not on the order path: nothing places an order except
`/nse-broker`, after `/nse-risk` passes and the user types an explicit yes for
that order.
