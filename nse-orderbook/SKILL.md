---
name: nse-orderbook
description: >-
  Order-Book Screen. Reads the order wins that NSE companies file as corporate
  announcements ("Bagging/Receiving of orders/contracts"), pulls the order value
  out of each filing's PDF, sizes the last 90 days of new orders against the
  company's annual revenue, and keeps only companies whose orders are coming in
  faster than usual AND whose revenue is actually growing, so the orders are being
  delivered. Ranks the rest, logs the picks so you can see later how they did, and
  backtests the whole screen month by month on past filings. Research, not a buy
  signal; it never places orders. Use when the user says "order book", "order wins",
  "which companies got big orders", "order inflow", "run the order screen",
  "backtest the order book strategy", or as a daily routine.
---

# Order-Book Screen

Companies that win big orders often grow revenue for years afterwards. Studies of US order backlogs found that markets react too slowly to a rising order inflow. The level of the backlog matters much less than the change. This skill turns that into a screen for NSE stocks.

Not financial advice. This is a research list built from public filings. It is not a recommendation to buy anything, and it never places orders. A big order can still be low-margin, delayed or cancelled. Nothing is placed without your yes: trading goes through /nse-risk and /nse-broker only.

## What it does

1. **Collects filings.** It pulls every NSE announcement with the category "Bagging/Receiving of orders/contracts", and subtracts "Rescission/termination(s)" as cancellations.
2. **Reads the order value.** It checks the announcement text first, then the attached PDF via `pdftotext`.
   - It prefers the "Broad consideration or size of the order" line from the SEBI disclosure table, then a sentence about the order.
   - It never just takes the biggest number. Turnover, EMD and bank-guarantee figures are skipped.
   - It handles crore, lakh, plain rupees, USD and EUR, and ranges such as "Rs 100-500 Cr" or L&T's "Mega*" classes (midpoint, marked `band`).
   - "Including GST" values are divided by 1.18 and flagged `gst`. L1 or lowest-bidder and letter-of-intent filings are flagged too.
3. **Screens.** A company passes only if all of these hold:
   - Orders in the last 90 days, net of cancellations, are at least 10% of its last annual revenue.
   - That is at least 1.25 times its usual 90-day pace over the 9 months before.
   - Its revenue grew at least 10% in the last reported year. In the daily run, its latest quarter must also be no lower than a year earlier. This is the "is it growing as per plan" check: orders that never turn into revenue are filtered out.
   - It is liquid: close at least Rs 50 and at least Rs 10 crore traded a day (60-day median).
   - Cancellations are under 25% of new orders.
   - In the daily run, it is not on NSE's ASM surveillance list.
4. **Ranks** by order size relative to revenue times acceleration, capped at 3x, and shows the top 10.

## Daily use

```bash
python3 nse-orderbook/orderbook.py daily            # new order filings today + the ranked list
python3 nse-orderbook/orderbook.py daily --write    # also save nse/orderbook-picks.json and log the picks
python3 nse-orderbook/orderbook.py track            # how every logged pick has done since
```

Run `daily --write` once a day, after 18:00 IST, because about half of all order filings arrive after the market closes. `track` is the honest test. After a few months it shows whether the picks actually beat the market.

## Backtest

```bash
python3 nse-orderbook/orderbook.py backtest --from 2024-11-01 --to 2026-10-28
```

The backtest replays the screen at the end of every month, using only what was known on that day:
- filings up to that day;
- revenue only after the year's results would have been out (60 days after the fiscal year ends);
- prices up to that day.

It buys each month's picks at the next open and holds each for 3 months, equal weight. Delivery charges are included: STT 0.1% both sides, stamp duty, exchange, SEBI, GST, and the DP charge per sell. It compares the result with Nifty 50.

The report is saved to `nse/backtest/orderbook-<from>_<to>.md`, with the equity curve as CSV and the monthly picks as JSON.

Read the caveats the report prints:
- Survivorship bias.
- PDF values parsed by pattern.
- An assumed reporting lag.
- No slippage.
- A small sample.

A good backtest is a reason to start paper-tracking, not a promise.

## Files

- `nse/cache/orderbook/` holds announcements, PDFs, PDF text, prices and revenue. Each is downloaded once.
- `nse/state/orderbook.json` holds the parsed orders, keyed by NSE's seq_id.
- `nse/orderbook-picks.json` is today's list. `nse/state/orderbook-track.json` is the pick log.

## Data sources

- NSE corporate announcements: `https://www.nseindia.com/api/corporate-announcements` (public, no login).
- Filing PDFs: `nsearchives.nseindia.com`.
- Yahoo Finance daily prices and annual and quarterly revenue (unofficial: check the company's own filing before relying on a number).
- Nifty 50 (`^NSEI`) as the benchmark.

Offline: `--offline DIR` reads `ann/`, `txt/`, `pdf/`, `px/` and `rev/` from DIR instead of the network.

## Settings

Put overrides under `"orderbook"` in `nse/config.json`, for example `{"orderbook": {"min_intensity": 0.15, "top_n": 5}}`. The keys are:
- `window_days`
- `history_days`
- `min_intensity`
- `min_accel`
- `min_rev_growth`
- `min_price`
- `min_median_value_cr`
- `top_n`
- `hold_months`
- `capital`
- `delivery_charges`
