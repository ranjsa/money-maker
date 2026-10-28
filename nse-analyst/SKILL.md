---
name: nse-analyst
description: >-
  The Earnings Analyst for NSE. Reads a listed company's reported quarterly
  numbers from Yahoo Finance fundamentals (revenue, gross, operating and net
  margins, net income, diluted EPS) and sums up what changed, quarter by
  quarter, each against the same quarter a year earlier, with the last four
  quarters added up. Shows Indian fiscal labels (Q1 FY27 is April to June 2026)
  and money in rupees crore, and links the company's latest results filing from
  NSE's Financial Results feed. Reads an earnings call transcript when you
  paste one. Use when the user says "how did Reliance's results look", "analyze
  the results", "what changed this quarter", "is revenue growing", "compare
  margins", "sum up the concall", "when does it report", or before trading a
  stock around its results.
argument-hint: "[symbol]"
---

# nse-analyst

Results move stocks more than anything else on the calendar, and the press
release is written to put the best number first. This skill lines up the
reported numbers quarter by quarter and says plainly what changed. For a day
trader it answers two questions before a trade: what did the last results say,
and does the next one fall inside this trade?

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/results.py" --help
```

## What it reads and writes

- Reads Yahoo Finance's fundamentals timeseries for `SYMBOL.NS` (quarterly
  revenue, gross profit, operating income, net income, diluted EPS) and NSE's
  `Financial_Results.xml` RSS for the link to the company's latest quarterly
  results filing. The company is matched to the feed by its exact name from
  `nse/universe.json` (made by `/nse-scanner`), so run the scanner's universe
  step once first. Without a name the table still prints and the filing link is
  skipped, with a note.
- Writes nothing. `--json` prints everything machine-readable.
- Earnings calls: the tool does not fetch transcripts. If the user pastes one
  (or the company's investor page has it), read it and sum up what management
  said changed: guidance, margins, demand, anything they declined to answer.
- The next results date is not in this data. Look for it in `/nse-news`
  announcements (board meeting intimations) or on nseindia.com, and say where
  it came from.

## Source caveat

Yahoo's fundamentals are an unofficial feed. They can lag a filing, differ
from the company's own press release (consolidated vs standalone), or have
gaps. The tool prints this on every run. Before the user acts on a number,
check it against the company's filing on NSE (the tool prints the link when it
finds one).

## The rules

- **Fiscal labels are Indian.** The year runs April to March. The quarter
  ending 30 June 2026 is Q1 FY27, 30 September is Q2, 31 December is Q3,
  31 March is Q4 (so 31 March 2025 is Q4 FY25).
- **Money is rupees crore**, printed like `₹19,407.0 cr` (1 crore is 10
  million). EPS is in rupees, two decimals.
- **Compare like with like.** Growth is against the same quarter about a year
  earlier. When that quarter is missing the answer is `n/a`, never 0.
  Quarter-on-quarter changes are shown but many businesses are seasonal.
- **Missing is `n/a`.** Banks and finance companies have no gross profit, so
  gross margin is `n/a`, not zero. A margin whose revenue or profit is missing
  is `n/a`.
- **Last four quarters** are added up only when all four are present and back
  to back. Otherwise it says `n/a`; do not estimate the gap.
- **Newest first**, six quarters by default (`--quarters 8` for two years).
  Only three-month periods count.
- **Flag one-offs.** When net income is above operating income, gains outside
  the business are inside it. The tool says so.

## Steps

1. Run it for the company in question (or each symbol on `nse/watchlist.json`):

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/results.py" RELIANCE
   python3 "${CLAUDE_SKILL_DIR}/results.py" HDFCBANK --quarters 8
   python3 "${CLAUDE_SKILL_DIR}/results.py" "M&M" --json
   ```

   If Yahoo has no data it prints a plain error and exits with status 1.
2. Open the filing link (and the company's results press release) when the
   numbers need context, and read it.
3. Write the summary: one line on the quarter (revenue, EPS, growth), one on
   margins, one on anything unusual (a one-off, a gap in the data, a stale
   quarter), and the links. Plain words, real numbers, no adjectives the
   numbers do not support.
4. If the next results fall inside the planned trade (an overnight hold, or a
   result due before the open), say it before `/nse-exit` sets the stop: a
   stop does not protect against a gap, the stock can open past it.

## Example

`results.py RELIANCE` (fixture run, Yahoo data): the quarter ending
2026-06-30 (Q1 FY27) shows revenue `₹3,09,468.0 cr`, +27.0% against a year
earlier, net income `₹20,946.0 cr` (-22.4%), diluted EPS `₹15.48`; gross
margin 26.8%. The quarter ending 2025-12-31 shows `n/a` for growth because
Yahoo has no December 2024 quarter, and the last-four-quarters line is `n/a`
because September 2025 is missing. The summary says so rather than filling
the gap.

## What it never does

- Never says a stock is cheap, expensive, a buy or a sell. It reports what
  the data says.
- Never invents a number the data does not have, or estimates a quarter.
- Never calls NSE more than a few times a minute, and sends the User-Agent
  `Mozilla/5.0 (nse-day-trader-skill)`.

## Safety

Not financial advice. Education and tooling only. This skill only reads
data. Nothing places an order except `/nse-broker`, after `/nse-risk` passes
and the user types an explicit yes for that order.
