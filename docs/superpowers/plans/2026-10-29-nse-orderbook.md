# nse-orderbook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 12th skill, `nse-orderbook`. It parses NSE order-win filings, ranks companies by order inflow accelerating relative to revenue, and keeps only those whose revenue is growing. It runs daily and backtests over Nov 2024 to Oct 2026.

**Architecture:** One tool, `nse-orderbook/orderbook.py`, with subcommands `fetch`, `parse`, `daily`, `scan`, `track` and `backtest`.
- It carries the shared common block, synced by `tests/sync_common.py`.
- Delivery charges live in the tool itself. The shared charges block is the intraday one, so it is not used.
- Raw data is cached under `nse/cache/orderbook/`.

**Tech Stack:** Python 3.8 stdlib, the optional `pdftotext` binary, NSE API, Yahoo chart and fundamentals-timeseries.

**Spec:** `docs/superpowers/specs/2026-10-29-nse-orderbook-design.md`

## Global Constraints

- Stdlib only. `pdftotext` is invoked via subprocess if it is on PATH; otherwise the tool falls back to description text and prints `pdftotext not found: order values read from filing text only`.
- UA `Mozilla/5.0 (nse-day-trader-skill)`, ≥0.25 s between NSE requests, every fetcher honours `--offline DIR`, cached responses are reused (never re-download a cached PDF).
- Paths: `nse/cache/orderbook/ann/<from>_<to>.json`, `nse/cache/orderbook/pdf/<seq_id>.pdf`, `nse/cache/orderbook/txt/<seq_id>.txt`, `nse/state/orderbook.json` (parsed orders), `nse/orderbook-picks.json`, `nse/state/orderbook-track.json`, `nse/backtest/orderbook-<from>_<to>.{md,csv,json}`.
- Thresholds (config `orderbook` section, these defaults): `window_days 90`, `history_days 270`, `min_intensity 0.10`, `min_accel 1.25`, `accel_cap 3`, `min_rev_growth 0.10`, `max_termination_share 0.25`, `revenue_lag_days 60`, `min_price 50`, `min_median_value_cr 10`, `liquidity_days 60`, `top_n 10`, `hold_months 3`, `capital 1000000`, `fx {"USD": 84.0, "EUR": 91.0}`, `gst_divisor 1.18`.
- Delivery charges defaults (`orderbook.delivery_charges`): `brokerage 0`, `stt_pct 0.1` both sides, `txn_pct 0.00307` both, `sebi_per_crore 10` both, `stamp_buy_pct 0.015`, `gst_pct 18` on brokerage+txn+sebi, `dp_per_sell 15.93`. Verify against zerodha.com/charges (equity delivery) in Task 3 and record the date.
- Categories: include `Bagging/Receiving of orders/contracts`; subtract `Rescission/termination(s)`; ignore all others.
- No em dashes; SKILL.md says "Not financial advice", that the list is research not a buy signal, and that it never places orders; frontmatter `name: nse-orderbook`, description > 200 chars, > 60 lines.
- The repo becomes 12 skills: update `tests/test_repo.py` SKILLS/ABOUT ("12 Claude skills for trading on NSE: ...") and `.claude-plugin` descriptions and README table. Keep install lines (`ranjsa/money-maker`).
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **PDF with several amounts** (contract value, EMD, bank guarantee, the company's own turnover). The parser must pick the order value or return `none`, not the largest number. Real-PDF fixtures in Task 1.
2. **Band and GST values**: "Rs 100-500 Cr" becomes 300 cr with `basis band`; "₹118 cr including GST" becomes 100 cr with the `gst` flag. Task 1.
3. **Look-ahead**: at signal date D, no order after D, no revenue with `FY end + 60 days > D`, and no price after D may be used. Task 3 asserts this with a poisoned future row.
4. **Missing prices or revenue** (delisted symbol, Yahoo 404): skip the stock with a counted reason, and never crash or pick it. Tasks 2 and 3.
5. **Repeat filers and duplicates**: the same seq_id, or the same PDF filed twice, is counted once. Task 1.

---

### Task 1: Fetch, PDF text and value parser (plus scaffold to 12 skills)

**Files:** Create `nse-orderbook/SKILL.md` (initial) and `nse-orderbook/orderbook.py`. Modify `tests/test_repo.py`, `.claude-plugin/*.json`, `README.md` (table row + count). Create `tests/test_orderbook_parse.py` and fixtures under `tests/fixtures/nse-orderbook/`: `ann-sample.json`, a trimmed real 15-day API response including both categories, and `txt/<seq>.txt`, real pdftotext outputs for at least 12 filings covering rupees in Indian grouping, crore, lakh, a band, GST-inclusive, USD, multiple amounts and no value. Store only text, no PDFs.

**Interfaces (produce):**
- `fetch_announcements(from_d: date, to_d: date, cache_dir, offline=None) -> list[dict]` (15-day chunks, cached)
- `order_rows(raw: list) -> list[dict]`: filters categories. Keys: `seq_id, symbol, name, kind ("order"|"termination"), at (ISO IST), desc, pdf_url`. Deduped by seq_id and by pdf_url.
- `pdf_text(row, cache_dir, offline=None) -> str|None`
- `parse_value(text: str) -> dict`: keys `value_inr (Decimal|None), basis ("text"|"pdf"|"band"|"none"), flags (sorted list from {"gst","l1","loi","fx","range"}), raw (matched snippet)`
- `parse_order(row, text) -> dict`: row + parse_value, using the description first, then the PDF text
- CLI `orderbook.py fetch --from YYYY-MM-DD --to YYYY-MM-DD [--offline DIR]` and `orderbook.py parse [--offline DIR]`, which update `nse/state/orderbook.json` (keyed by seq_id, idempotent)

- [ ] Step 1: Write failing tests.
  - Each fixture txt has a pinned expected `value_inr` and basis/flags, hand-checked against the text.
  - `parse_value("Rs. 3,13,57,320")` gives 31357320.
  - `"₹2,225 Crores"` gives 22250000000.
  - `"Rs.6.75 crores"` gives 67500000.
  - `"Large Order: Rs 100-500 Cr"` gives 3000000000 with basis band and flag range.
  - `"Rs 118 crore (including GST)"` gives 1000000000 with flag gst.
  - `"USD 12 million"` gives 12e6*84 with flag fx.
  - A text with no amount gives None and basis none.
  - A text where the turnover or EMD figure is larger than the order value gives the order value.
  - order_rows filters the categories, sets kind, and dedupes.
  - Add the test_repo 12-skill assertions.
- [ ] Step 2: Run `cd tests && python3 -m unittest test_orderbook_parse test_repo -v`. Expected: FAIL.
- [ ] Step 3: Implement. Value selection order:
  1. Annexure-A "size"/"value" line.
  2. A sentence containing order/contract/value/worth/aggregating.
  3. Else `none`.
  Never default to the largest number in the document.
- [ ] Step 4: Run the tests. Expected: PASS. Run `python3 tests/sync_common.py --check`.
- [ ] Step 5: Commit `feat(orderbook): fetch NSE order filings and parse order values`.

### Task 2: Signal, execution filter, daily screen and tracking

**Files:** Modify `nse-orderbook/orderbook.py` and `SKILL.md`. Create `tests/test_orderbook_signal.py` with synthetic fixtures (orders, annual and quarterly revenue, daily bars).

**Interfaces:**
- Consumes `nse/state/orderbook.json` rows from Task 1.
- `annual_revenue(sym, offline=None) -> list[(fy_end: date, value: Decimal)]`, `quarterly_revenue(sym, offline=None)`, `daily_bars(sym, start, end, offline=None) -> list[dict(date, open, close, volume)]`, all cached.
- `revenue_at(annual, d, lag_days=60) -> (value, growth|None)`: latest FY with end+lag ≤ d, and growth vs the prior FY.
- `signal(orders_for_sym, d, cfg) -> dict`: keys `inflow90, prior_avg90, intensity, accel, gross, terminations, n_orders, n_valued`.
- `screen(d, orders, data_provider, cfg, live=False) -> (picks: list[dict], rejected: dict[str, list])`. Picks carry `symbol, score, intensity, accel, rev, rev_growth, median_value_cr, close, orders (list of seq_id, value, basis, flags)`. Rejected maps a reason (`no_value`, `low_intensity`, `no_accel`, `rev_not_growing`, `qtr_falling`, `terminations`, `illiquid`, `price`, `asm`, `no_price_data`, `no_revenue`) to symbols.
- CLI:
  - `orderbook.py daily [--offline DIR] [--write]`: fetches from the last fetched date (in state) to today, parses, screens as of today, and prints new valued orders today plus the top_n table. `--write` writes `nse/orderbook-picks.json` and appends to the track log.
  - `orderbook.py scan --date YYYY-MM-DD`: screens as of that date.
  - `orderbook.py track`: shows each logged pick's return since its pick date, with the median and hit rate vs Nifty.

- [ ] Step 1: Write failing tests on synthetic data.
  - Intensity and accel arithmetic.
  - A company with orders of 20% of revenue and revenue growth of 5% is rejected `rev_not_growing`.
  - Orders inside D-90 count; one at D+1 does not.
  - A FY ending 50 days before D is not used (the lag).
  - A termination share of 30% gives `terminations`.
  - Median value under ₹10 cr gives `illiquid`.
  - No accel history passes accel.
  - The score caps accel at 3.
  - `daily --offline` prints the table and is idempotent, so a second run adds no duplicate track rows.
- [ ] Step 2: Run, expect FAIL. Step 3: implement. Step 4: run, expect PASS.
- [ ] Step 5: Commit `feat(orderbook): order-inflow screen with execution filter, daily and track`.

### Task 3: Backtest and the real 2-year run

**Files:** Modify `nse-orderbook/orderbook.py` and `SKILL.md`. Create `tests/test_orderbook_backtest.py`. Write output to `nse/backtest/...` at run time, and commit only `docs/orderbook-backtest-2024-11_2026-10.md`, which holds the report text from the real run.

**Interfaces:**
- `delivery_charges(buy_value, sell_value, sells=1, cfg=None) -> Decimal`.
- `backtest(start: date, end: date, orders, data_provider, cfg) -> dict`. Keys:
  - `equity` (list of (date, value))
  - `bench` (dict name -> list)
  - `trades` (list)
  - `picks_by_month`
  - `stats`: `total_return, cagr, max_dd, months_beat_nifty, n_trades, avg_holdings, charges, coverage_pct, skipped (reason -> count)`
- Rules:
  - Signal on the last trading day of each month, using `screen(..., live=False)`.
  - Enter at the next session's open. Each pick is held 3 months.
  - Monthly rebalance to equal weight across the union of active picks, with cash if there are none.
  - Charges are applied on every buy and sell.
  - Marked to market daily on closes.
- CLI `orderbook.py backtest --from 2024-11-01 --to 2026-10-28 [--offline DIR]`. It fetches filings from from-365 days. It writes md, csv and json, and prints the report with an ASCII equity chart and the caveats block from the spec.

- [ ] Step 1: Write failing tests on a synthetic 6-month world.
  - Two stocks with known prices. The equity curve matches a hand computation, including charges (pin the numbers).
  - **Look-ahead guard:** inject a huge order dated after D, a revenue row with FY end+lag > D, and a price row after the exit. None may change the picks or the P&L.
  - A missing price symbol is skipped and counted.
  - `delivery_charges(100000, 110000)` is pinned per the defaults.
- [ ] Step 2: Run, expect FAIL. Step 3: implement. Step 4: run, expect PASS.
- [ ] Step 5: Verify the delivery rates at zerodha.com/charges and update the defaults if they differ.
- [ ] Step 6: Do the real run, live and in the background if it is long: `python3 nse-orderbook/orderbook.py backtest --from 2024-11-01 --to 2026-10-28`.
  - Commit `docs/orderbook-backtest-2024-11_2026-10.md` with the report.
  - Report parse coverage, and how many filings had a value via text, PDF or band.
  - If coverage is under 60%, say so prominently.
- [ ] Step 7: Commit `feat(orderbook): point-in-time backtest with delivery charges; 2-year report`.

### Task 4: Docs and final pass

**Files:** Modify `README.md` (an nse-orderbook section covering the daily run, the backtest summary with the honest caveats and a link to the report) and `nse-orderbook/SKILL.md` (triggers: "order book", "order wins", "which companies got big orders", "run the order screen"; a daily-schedule tip).
- [ ] Step 1: The README test asserts that the backtest numbers in README equal those in the committed report.
- [ ] Step 2: Run the full suite with 0 skipped, plus sync check.
- [ ] Step 3: Commit `docs(orderbook): README and skill guide`.
