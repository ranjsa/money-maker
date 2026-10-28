# nse-orderbook: order-inflow screen (design)

## Intent

The user wants a daily skill. It reads companies' order-win filings on NSE and works out how big each new order is next to the company's revenue. It keeps only companies that are actually delivering, meaning revenue is growing as the orders come in. The result is a list of stocks whose order inflow is speeding up. The user also wants the strategy backtested over the past 2 years, with the profit it would have made.

Research (2026-10-29) shapes it:
- The literature supports the *change* in order inflow relative to the company's size, held for months.
- A big backlog level by itself is not a good sign.
- Day-of-announcement trading is mostly priced in by the next open.
- Only about 12% of NSE filings state the order value in the text, so the attached PDF must be parsed.
- There is no Indian backtest of this, so the skill has to measure its own picks.

## Goals

- `daily`: pull new filings and parse their values, then print today's material orders and the current ranked list. Run once a day.
- `backtest`: replay the screen month by month over Nov 2024 to Oct 2026 using only data known at each date, then report profit after delivery charges against Nifty 50.
- `track`: log each pick with its date and price, and show how the picks have done since.

## Non-goals

- Exchange bid/ask order-book depth.
- Intraday trading on announcements.
- Placing orders. This skill only lists stocks; nse-broker remains the only path to an order.
- Margin or profitability of orders, which filings do not disclose.

## Anti-goals (failure even if it runs)

- Micro-caps or pump stocks in the list.
- Treating an L1, LoI, DAC approval or GST-inclusive figure as firm revenue without flagging it.
- Look-ahead in the backtest.
- A profit number shown without its caveats.

## Decisions (controller's calls; the user did not specify these)

- **Sources:**
  - NSE `/api/corporate-announcements` in 15-day chunks, using category `Bagging/Receiving of orders/contracts`.
  - Terminations, category `Rescission/termination(s)`, are subtracted.
  - `Awarding of order(s)/contract(s)` is ignored because it is ambiguous.
  - Order values come from `pdftotext -layout` run on the attached PDF, with a fallback to the description text. If pdftotext is missing, only the description text is used and the tool says so.
- **Value parse:**
  - Units: Rs, ₹, INR, crore, cr, lakh, million, bn; plain rupees in Indian grouping; ranges (midpoint, flagged `band`); USD and EUR via config FX.
  - "Including GST" values are divided by 1.18 and flagged.
  - Each order records `value_inr`, `basis` (`text`, `pdf`, `band` or `none`) and flags (`gst`, `l1`, `loi`, `fx`).
- **Signal at date D (point in time):**
  - `inflow90` = sum of valued orders minus terminations in (D-90, D].
  - Revenue base = latest annual revenue whose fiscal year ended at least 60 days before D (Yahoo `annualTotalRevenue`).
  - `intensity` = inflow90 / revenue. It must be at least 0.10.
  - `accel` = inflow90 / (average 90-day inflow over the 270 days before the window). It must be at least 1.25. No prior orders counts as acceleration.
- **Execution ("growing as per plan") filter:**
  - Latest known annual revenue growth must be at least 10%. In live mode, the latest quarterly revenue must also be at least the same quarter a year earlier, where Yahoo has it.
  - Drop a company if terminations in the window are 25% or more of gross inflow.
- **Liquidity and quality:**
  - EQ series only.
  - Close at least ₹50.
  - Median daily traded value over the prior 60 sessions at least ₹10 cr.
  - Live mode also excludes NSE's ASM list.
- **Rank:** score = intensity × min(accel, 3). Show the top 10.
- **Backtest:**
  - Universe is every symbol that filed an order. Signals are taken at the last trading day of each month and entered at the next session's open.
  - Each month's picks are held for 3 months. The portfolio is the equal-weighted union of the last 3 months' picks, rebalanced monthly, starting from ₹10,00,000 notional.
  - Delivery charges per Zerodha's delivery schedule (config, verify at build): brokerage 0; STT 0.1% on buy and sell; exchange 0.00307%; SEBI ₹10/cr; stamp 0.015% on buy; GST 18% on (brokerage + exchange + SEBI); DP ₹15.93 per sell.
  - Benchmarks: ^NSEI, and ^CRSLDX (Nifty 500) if available.
  - Report: total return, CAGR, max drawdown, months beating Nifty, trades, average holdings, charges paid, parse coverage %, per-pick log, equity curve CSV, and an ASCII chart.
  - Order filings are fetched from Nov 2023 so the first signals have full lookback.
- **Caveats printed with every backtest:**
  - Survivorship: the universe is built from filings, but price data exists only for symbols Yahoo still serves.
  - Value-parse error rate.
  - The 60-day revenue reporting lag is an assumption.
  - No slippage.
  - Small sample.
  - Not financial advice.
- **Shape:** folder `nse-orderbook/` with `SKILL.md` and `orderbook.py`. It is stdlib only apart from the optional `pdftotext` binary, Python 3.8, carries the shared common block, and writes to `nse/`. Cache goes in `nse/cache/orderbook/` and state in `nse/state/orderbook.json`. Every fetcher takes `--offline DIR`. The repo becomes 12 skills.

## Left to the builder

- Exact regex set and how to pick the order's value when a PDF mentions several amounts. Prefer the Annexure-A "size" line, then the amount nearest "order", "contract" or "value". Test this on real PDFs.
- Report layout.
