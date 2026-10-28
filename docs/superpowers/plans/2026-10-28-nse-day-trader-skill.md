# NSE Day Trader Skill Pack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port Jake Schincariol's `day-trader-skill` (11 Claude skills, MIT) to Indian cash equities on NSE: scanner, news, entry, exit, time machine, budget, risk, broker, alerts, analyst, and a bulk/block/insider-deals tracker replacing the upstream Congress-trades tracker.

**Architecture:** Fork the upstream repo and keep its shape: one folder per skill (`SKILL.md` + one stdlib-only Python tool), skills chained through JSON files in a project `nse/` folder, offline `unittest` suite on fixtures. Swap every US-specific piece (US Eastern clock, US exchange calendar, Nasdaq symbol lists, SEC filings, the US broker MCP, House Clerk, PDT) for its Indian counterpart (IST, NSE calendar, NSE archives, NSE announcements RSS, Zerodha Kite MCP + a local paper engine, bulk/block deals, SEBI-era intraday rules). Add two things the US pack lacks because India needs them: per-price tick sizes and statutory per-trade charges.

**Tech Stack:** Python 3.8+ standard library only (no pip). `unittest`. Data: Yahoo Finance chart/spark/fundamentals JSON (`.NS` symbols), NSE archives CSV/RSS, NSE `holiday-master` API, Google News RSS. Broker: Zerodha `kite-mcp-server` (official, Go) for live; built-in paper engine for paper.

**Spec:** No separate spec. Upstream repo is the behavioural spec (clone: `git clone https://github.com/Jakeschincariol/day-trader-skill`, read each `SKILL.md` and script docstring). India-specific requirements are fixed in Global Constraints below; where this plan is silent, match upstream behaviour.

## Global Constraints

- License: keep upstream MIT `LICENSE`, add `Copyright (c) 2026 Tushar Jha` line; README credits upstream with link.
- Repo name `nse-day-trader-skill`; plugin name `nse-day-trader-skill`; skill folders prefixed `nse-`: `nse-scanner nse-news nse-entry nse-exit nse-timemachine nse-risk nse-budget nse-broker nse-alerts nse-analyst nse-deals` (11).
- Working folder in user's project: `nse/` (config `nse/config.json`, state `nse/state/*.json`). Never the upstream `trader` folder, so both packs can coexist.
- Python 3.8+, standard library only. Every tool runnable as `python3 <folder>/<tool>.py`.
- Clock: IST is fixed UTC+05:30, no DST. All user-facing times IST, 24h `HH:MM` in config, `10:15 AM IST` in prose.
- Session (NSE CM): pre-open 09:00-09:08, normal 09:15-15:30. Opening range default 15 min = 09:15-09:30 (three 5m bars).
- Config defaults: `no_entries_before "09:30"`, `no_entries_after "14:45"`, `flat_by "15:10"` (before broker MIS auto square-off ~15:20), `product "MIS"`, `exchange "NSE"`, `budget 100000`, `max_loss_per_trade_inr 1000`, `max_daily_loss_inr 3000`, `max_trades_per_day 3`, `max_orders_per_day 10`, `max_order_inr 80000`, `min_order_inr 500`, `cash_buffer_pct 10`, `max_position_pct 75`, `min_reward_risk 1.0`, `require_stop true`, `allow_leverage false`, `allow_short false`, `allow_fno false`, `paper true`, `broker "paper"`, `blocked []`.
- 2026 NSE CM trading holidays (weekdays only; from `https://www.nseindia.com/api/holiday-master?type=trading`, key `CM`, fetched 2026-10-28): 2026-01-15, 01-26, 03-03, 03-26, 03-31, 04-03, 04-14, 05-01, 05-28, 06-26, 09-14, 10-02, 10-20, 11-10, 11-24, 12-25. Muhurat session 2026-11-08 (Sunday), timings TBA by NSE: config key `special_sessions: {"2026-11-08": ["18:00","19:00"]}` user-editable; no default entry.
- Year outside `CALENDAR_YEARS = (2026,)`: tools treat Mon-Fri as open and print `NSE calendar for <year> not loaded: run <tool> calendar --refresh or check nseindia.com`.
- Tick size (NSE CM, revision effective 2025-04-15, monthly review): price < 250 -> 0.01; 250-1000 -> 0.05; >1000-5000 -> 0.10; >5000-10000 -> 0.50; >10000-20000 -> 1.00; >20000 -> 5.00. Slab chosen by the reference price (entry). Stops round DOWN to tick, targets round to nearest tick, never toward entry.
- Charges (intraday equity, all user-editable under `charges` in config; defaults Zerodha-style): brokerage `min(20, 0.03% x turnover)` per executed order; STT 0.025% sell side; exchange txn 0.00307% both sides; SEBI ₹10 per crore both sides; stamp 0.003% buy side; GST 18% on (brokerage + exchange txn + SEBI). Round each component to paisa, total to paisa.
- Universe: NSE `EQUITY_L.csv`, series `EQ` only (BE/BZ are trade-to-trade, no intraday). Yahoo symbol = `SYMBOL + ".NS"`; `&` and `-` kept as-is (`M&M.NS`, `BAJAJ-AUTO.NS`).
- Money display: `₹` with Indian grouping (`₹1,23,456.78`); large figures in crore (`₹1,940.7 cr`).
- Long side only for v1 (upstream parity). Shorts, F&O, leverage stay blocked by risk unless config flag flips; flags exist but only `allow_short` gating is tested.
- Safety copy every order-path SKILL.md must carry (tested): "Not financial advice", and that nothing is placed without the user typing yes. README must state SEBI's FY22-FY24 study finding: 9 in 10 individual F&O traders lost money; intraday cash traders majority loss-making (cite SEBI study July 2023 / Sept 2024).
- No em dashes in non-fixture text (upstream rule, keep test). No personal email in repo.
- Network politeness: User-Agent `Mozilla/5.0 (nse-day-trader-skill)`, ≥0.25 s between NSE requests, every fetcher has `--offline DIR` that reads fixtures instead.

## Review Focus

1. **Holiday / weekend / out-of-calendar dates.** User runs scanner or entry on 2026-11-10 (Diwali-Balipratipada) or a 2027 date. Expect "market closed: <holiday>" or the calendar-not-loaded warning, never a stale "waiting". Test in Task 1 and Task 4.
2. **Stock at or near upper circuit.** Price-band stocks (2%/5%/10%/20% from `sec_list.csv`) can lock; a breakout entry there cannot be exited. Expect risk BLOCK `circuit` when entry ≥ upper band − 1 tick-slab-%. Test in Task 8.
3. **Charges eat a tight stop.** On a ₹180 stock with a ₹0.40 stop, round-trip charges can exceed half the risk. Expect budget to size on (loss at stop + charges) and exit/timemachine to print net R. Test in Tasks 2, 7.
4. **Symbols with `&` / `-` and non-EQ series.** `M&M`, `BAJAJ-AUTO`, `J&KBANK`; BE-series stock fed manually. Expect correct URL-encoding (`M%26M.NS`) and risk BLOCK `series`. Test in Tasks 3, 8.
5. **Stop/target leg orphaned after one fills (live Kite).** Kite has no bracket orders for retail; entry, SL-M and target LIMIT are separate. Expect broker `fill` of one leg to print the exact cancel ticket for the other and alerts to ring if both legs still open after a fill. Test in Task 9.

---

## File Structure

```
.claude-plugin/plugin.json, marketplace.json   renamed manifests
LICENSE, README.md
nse-scanner/      SKILL.md, scan.py                 universe (EQUITY_L), daily setups, ORB
nse-news/         SKILL.md, news.py                 Google News RSS + NSE announcements RSS + day move
nse-entry/        SKILL.md, entry.py                ORB / level trigger, bar by bar
nse-exit/         SKILL.md, exit.py                 stop/target, tick rounding, net R
nse-timemachine/  SKILL.md, timemachine.py          10,000 replays + history, net of charges
nse-budget/       SKILL.md, size.py, config.example.json   sizing incl. charges, Kite order legs
nse-risk/         SKILL.md, check.py                PASS/BLOCK, circuit, series, window
nse-broker/       SKILL.md, ticket.py, paper.py, claude_desktop_config.example.json
nse-alerts/       SKILL.md, alerts.py, alerts.example.json
nse-analyst/      SKILL.md, results.py              Yahoo fundamentals timeseries, INR crore
nse-deals/        SKILL.md, deals.py                bulk.csv, block.csv, PIT insider XBRL
tests/            _load.py, test_*.py, fixtures/<skill>/..., capture.py
```

Shared code: upstream copies its clock helpers into every script so each folder installs alone. Keep that. One block, delimited by `# --- nse common: begin ---` / `# --- nse common: end ---`, holds IST, calendar, tick, INR formatting and is byte-identical in every tool; `test_repo.py` enforces identity. A second block `# --- nse charges ---` is identical across `size.py`, `check.py`, `exit.py`, `timemachine.py`, `paper.py`. Edit the block in `nse-exit/exit.py` (the master copy), then run `python3 tests/sync_common.py` to copy it.

---

### Task 0: Fork and rename the scaffold

**Files:**
- Create: repo from upstream clone; rename `trader-*` -> `nse-*` folders, `trader-pelosi` -> `nse-deals`, `ptr.py` -> `deals.py`, `facts.py` -> `results.py`
- Modify: `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `LICENSE`, `tests/test_repo.py`
- Create: `tests/sync_common.py`
- Delete: upstream fixtures (US data) once each task replaces them

**Interfaces:**
- Produces: `tests/_load.py::load(folder, name)` (unchanged); `SKILLS` list in `test_repo.py` = the 11 `nse-*` names; `sync_common.py` CLI: `python3 tests/sync_common.py [--check]` (exit 1 if blocks differ, with `--check`).

- [ ] **Step 1:** `git clone https://github.com/Jakeschincariol/day-trader-skill nse-day-trader-skill && cd nse-day-trader-skill && git remote rename origin upstream && git checkout -b nse-port`
- [ ] **Step 2: Rewrite `tests/test_repo.py`** with `SKILLS` as above, `ORDER_PATH = [nse-scanner, nse-entry, nse-exit, nse-timemachine, nse-budget, nse-deals, nse-risk, nse-broker, nse-alerts]`, `ABOUT = "11 Claude skills for day trading on NSE: scan every EQ chart, track exchange filings and news, time every entry and exit, and replay every outcome before you risk a rupee."`, plugin name assertions `nse-day-trader-skill`, README must contain `/plugin marketplace add <owner>/nse-day-trader-skill`. Add:
  ```python
  def test_common_blocks_identical(self):
      self.assertEqual(subprocess.call([sys.executable, "tests/sync_common.py", "--check"], cwd=ROOT), 0)
  def test_no_us_leftovers(self):  # outside tests/fixtures and README credits
      for word in (the upstream US place, exchange, filing system, broker, tracker and folder names): ...assertNotIn
  ```
- [ ] **Step 3:** Run `python3 -m unittest tests.test_repo -v`. Expected: FAIL (folders not renamed).
- [ ] **Step 4:** `git mv` folders/files, update manifests (`name`, `description = ABOUT`, keywords `nse`, `india`, `zerodha`, `paper-trading`), `sync_common.py` (find block markers, copy master text into each listed file, `--check` diffs). Other test files: mark `@unittest.skip("ported in Task N")` until their task.
- [ ] **Step 5:** Run `python3 -m unittest tests.test_repo -v`. Expected: PASS except `test_no_us_leftovers` and `test_common_blocks_identical` (skip both with reason "Task 1" until Task 1 lands, then unskip there).
- [ ] **Step 6:** Commit `chore: fork day-trader-skill as nse-day-trader-skill`.

### Task 1: Common block (IST, NSE calendar, tick, INR)

**Files:**
- Modify: `nse-exit/exit.py` (master common block), then `sync_common.py` copies into all 11 tools
- Test: `tests/test_common.py`

**Interfaces:**
- Produces (in every tool):
  - `IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))`
  - `to_ist(utc: datetime) -> datetime`, `ist_to_utc(local_naive: datetime) -> datetime`, `parse_now(text: str) -> datetime` (UTC; accepts `"YYYY-MM-DD HH:MM"` as IST)
  - `NSE_HOLIDAYS: dict[str,str]` (iso -> name), `CALENDAR_YEARS = (2026,)`, `OPEN=09:15`, `CLOSE=15:30`, `PREOPEN=09:00`
  - `trading_day(d: date, cfg: dict|None=None) -> tuple[bool, str]` (reason: `"weekend"`, holiday name, `"special session"`, `"calendar not loaded"` (still True), `""`)
  - `session(d: date, cfg=None) -> tuple[time, time] | None`
  - `market_status(now_utc, cfg=None) -> dict` keys `state` in `{"pre-open","open","closed"}`, `reason`, `next_open` (ISO IST)
  - `tick_for(price: Decimal|float) -> Decimal`; `floor_tick(x, ref) -> Decimal`; `round_tick(x, ref) -> Decimal`; `ceil_tick(x, ref) -> Decimal`
  - `inr(x, decimals=2) -> str` (Indian grouping), `crore(x) -> str`
  - `yahoo_symbol(sym: str) -> str` (`"M&M"` -> `"M&M.NS"`), `yahoo_quote(sym) -> str` (URL-encoded, `"M%26M.NS"`)

- [ ] **Step 1: Write failing tests** in `tests/test_common.py` (load via `load("nse-exit","exit")`):
  ```python
  self.assertEqual(to_ist(datetime(2026,10,27,3,45,tzinfo=utc)).strftime("%H:%M"), "09:15")
  self.assertEqual(trading_day(date(2026,11,10)), (False, "Diwali-Balipratipada"))
  self.assertEqual(trading_day(date(2026,10,24))[0], False)          # Saturday
  self.assertEqual(trading_day(date(2027,1,4)), (True, "calendar not loaded"))
  self.assertEqual(trading_day(date(2026,11,8), {"special_sessions": {"2026-11-08": ["18:00","19:00"]}}), (True, "special session"))
  self.assertEqual(market_status(parse_now("2026-10-27 09:05"))["state"], "pre-open")
  self.assertEqual(market_status(parse_now("2026-10-27 15:31"))["next_open"], "2026-10-28T09:15+05:30")
  self.assertEqual(market_status(parse_now("2026-11-09 16:00"))["next_open"], "2026-11-11T09:15+05:30")
  for p, t in [(249.99,"0.01"),(250,"0.05"),(1000,"0.05"),(1000.05,"0.10"),(5000.5,"0.50"),(15000,"1.00"),(25000,"5.00")]:
      self.assertEqual(tick_for(p), Decimal(t))
  self.assertEqual(floor_tick(Decimal("1005.25"), Decimal("1015")), Decimal("1005.20"))
  self.assertEqual(inr(12345678.9), "₹1,23,45,678.90"); self.assertEqual(crore(1.9407e11), "₹19,407.0 cr")
  self.assertEqual(yahoo_quote("M&M"), "M%26M.NS"); self.assertEqual(yahoo_symbol("BAJAJ-AUTO"), "BAJAJ-AUTO.NS")
  ```
- [ ] **Step 2:** Run `python3 -m unittest tests.test_common -v`. Expected: FAIL (`AttributeError: to_ist`).
- [ ] **Step 3:** Implement the block in `nse-exit/exit.py` replacing upstream `_nth_sunday`/`eastern_*`. Tick boundaries: inclusive upper bound per slab (`250 -> 0.05` means `<250` is 0.01; `1000 -> 0.05`, `>1000 -> 0.10`). Use `Decimal` throughout. `calendar` subcommand on `nse-alerts/alerts.py` only (Task 10) does the live refresh; block holds the static dict.
- [ ] **Step 4:** `python3 tests/sync_common.py` then `python3 -m unittest tests.test_common tests.test_repo -v`. Expected: PASS (unskip the two Task-0 repo tests).
- [ ] **Step 5:** Commit `feat: IST clock, NSE calendar, tick sizes, INR formatting`.

### Task 2: Charges block

**Files:**
- Modify: `nse-exit/exit.py` (master `# --- nse charges ---` block), synced to `size.py`, `check.py`, `timemachine.py`, `paper.py`
- Test: `tests/test_charges.py`

**Interfaces:**
- Produces: `CHARGES_DEFAULT: dict` (keys `brokerage_flat 20`, `brokerage_pct 0.03`, `stt_sell_pct 0.025`, `txn_pct 0.00307`, `sebi_per_crore 10`, `stamp_buy_pct 0.003`, `gst_pct 18`); `charges(buy_value: Decimal, sell_value: Decimal, cfg_charges: dict|None=None) -> dict` with keys `brokerage, stt, txn, sebi, stamp, gst, total` (Decimal, paisa); `round_trip(qty: int, entry: Decimal, exit_: Decimal, cfg=None) -> Decimal` (= `charges(qty*entry, qty*exit_)["total"]`).

- [ ] **Step 1: Failing tests:**
  ```python
  c = charges(Decimal("100000"), Decimal("101000"))
  self.assertEqual(c["brokerage"], Decimal("40.00"))   # 20 per side, 0.03% would be 30/30.30
  self.assertEqual(c["stt"], Decimal("25.25"))         # 0.025% of 101000
  self.assertEqual(c["stamp"], Decimal("3.00"))
  self.assertEqual(c["total"], c["brokerage"]+c["stt"]+c["txn"]+c["sebi"]+c["stamp"]+c["gst"])
  self.assertEqual(charges(Decimal("5000"), Decimal("5000"))["brokerage"], Decimal("3.00"))  # 0.03% < 20
  self.assertEqual(charges(Decimal("1000"),Decimal("1000"),{"brokerage_flat":0,"brokerage_pct":0})["brokerage"], Decimal("0.00"))
  ```
- [ ] **Step 2:** Run `python3 -m unittest tests.test_charges -v`. Expected: FAIL.
- [ ] **Step 3:** Implement; round each component `ROUND_HALF_UP` to 0.01 before summing; GST on rounded components. Config `charges` overrides key by key.
- [ ] **Step 4:** Before PASS, verify defaults against `https://zerodha.com/charges` (equity intraday tab) and update `CHARGES_DEFAULT` and Global Constraints if they differ; note date checked in a comment. Sync, run tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: statutory intraday charges model`.

### Task 3: nse-scanner

**Files:**
- Modify: `nse-scanner/scan.py`, `nse-scanner/SKILL.md`
- Test: `tests/test_scanner.py`; fixtures `tests/fixtures/nse-scanner/EQUITY_L.csv` (trimmed to ~40 rows incl. `M&M`, `BAJAJ-AUTO`, one `BE`), `spark-5m-<day>.json.gz`, `daily-spark-<day>.json`
- Create: `tests/capture.py` (one CLI used by every later task to snapshot live endpoints into fixtures: `python3 tests/capture.py <skill> --day YYYY-MM-DD`)

**Interfaces:**
- Consumes: common block.
- Produces: `parse_equity_list(text) -> list[dict]` (`symbol, name, series, isin, listed`); `build_universe(text) -> list[str]` (EQ only, sorted); `scan_orb(...)`/`scan_daily(...)` same signatures as upstream; writes `nse/watchlist.json` `{"created_by":"nse-scanner","date","setup","symbols":[{"symbol","yahoo","or_high","or_low","close","pct_over","level"?}]}`; `nse/universe.json` `{"as_of","symbols":[...],"names":{sym:name}}` (names used by nse-news).

- [ ] **Step 1: Failing tests:** universe from fixture excludes the BE row and includes `M&M`; `yahoo_quote` used in spark path (`"symbols=M%26M.NS,..."`); ORB on synthetic DEMO bars (OR 09:15-09:30 high 1012.40 low 998.10, the 09:30-09:35 bar closes 1015.00) at `--at 09:35` lists DEMO with `pct_over 0.26`; `--at 09:25` refuses with "opening range not complete until 09:30 IST"; on 2026-11-10 prints `market closed: Diwali-Balipratipada`; a real-fixture smoke test asserts the ORB count equals what `capture.py` recorded in `expected.json`.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_scanner -v`. Expected: FAIL.
- [ ] **Step 3:** Port: replace Nasdaq symdir with `https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv` (columns have leading spaces: strip headers); spark batches of 20 `.NS` symbols; default filters `min_price 20`, `min_turnover_inr 5_00_00_000` (₹5 cr daily) instead of share volume; drop US ADR logic. Write `capture.py` (generic: list of `(url, fixture_name)` per skill, gzip >200 KB, writes `expected.json` by running the tool on the fixtures).
- [ ] **Step 4:** `python3 tests/capture.py nse-scanner --day 2026-10-27` (live, once), then run tests. Expected: PASS.
- [ ] **Step 5:** Update SKILL.md: triggers ("scan NSE", "Nifty breakouts", "what's breaking out today"), IST times, `nse/` paths. Commit `feat(scanner): NSE EQ universe and ORB scan`.

### Task 4: nse-entry

**Files:** Modify `nse-entry/entry.py`, `SKILL.md`; Test `tests/test_entry.py`; fixtures `DEMO-5m-synthetic.json`, one captured real session.

**Interfaces:**
- Consumes: `nse/watchlist.json` (Task 3), common block.
- Produces: `nse/entry.json` = upstream `entry_doc` shape with `"source":"yahoo"`, `"exchange":"NSE"`, times as IST `"09:35 AM"`; `evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m", level=None, ...) -> dict` (status in `waiting|triggered|expired|closed|not_open`).

- [ ] **Step 1: Failing tests** on DEMO: at `09:33` -> `waiting` (09:30 bar forming); at `09:35` -> `triggered`, `price 1015.00`, `time "09:35 AM"`; after `no_entries_after 14:45` with no trigger -> `expired`; `now 2026-11-10 10:00` -> `closed` with reason holiday; `now 2026-10-27 09:05` -> `not_open` with `opens 09:15 AM IST`.
- [ ] **Step 2:** Run, expect FAIL.
- [ ] **Step 3:** Port clock/session calls to common block; remove US early-close logic; Yahoo `.NS` via `yahoo_quote`.
- [ ] **Step 4:** Run, expect PASS.
- [ ] **Step 5:** Commit `feat(entry): IST bar-by-bar trigger`.

### Task 5: nse-exit

**Files:** Modify `nse-exit/exit.py`, `SKILL.md`; Test `tests/test_exit.py`.

**Interfaces:**
- Consumes: `nse/entry.json`.
- Produces: `nse/plan.json` = upstream plan shape plus `"tick"` (str), `"est_charges_per_share"` (float, `round_trip(CHARGE_REF_QTY, entry, target) / CHARGE_REF_QTY`), `"net_reward_risk"` (float), `"exchange":"NSE"`, `"product":"MIS"`; `compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None, atr=None, atr_n=None) -> dict` with Decimal `stop` floored to tick, `target` rounded away from entry to tick.

- [ ] **Step 1: Failing tests:** DEMO entry 1015.00, OR 1012.40/998.10, `--stop or-mid --target 2R` -> stop `1005.20` (1005.25 floored to 0.10), risk `9.80`, target `1034.60`; stock at 180.00 with `--stop price:179.60 --target 1R` -> refuses with "charges ≈ ₹X/share are over half the ₹0.40 risk/share: widen the stop or skip" whenever `est_charges_per_share > 0.5 * risk_per_share`; `flat_by` defaults to config `15:10`; `--flat-by 15:25` warns "after 15:20 your broker may auto square-off MIS".
- [ ] **Step 2:** Run, expect FAIL.
- [ ] **Step 3:** Implement. Reference qty for per-share charges: 100 shares (documented constant `CHARGE_REF_QTY = 100`); budget recomputes exactly at real qty.
- [ ] **Step 4:** Run, expect PASS.
- [ ] **Step 5:** Commit `feat(exit): tick-rounded stops and net R`.

### Task 6: nse-timemachine

**Files:** Modify `nse-timemachine/timemachine.py`, `SKILL.md`; Test `tests/test_timemachine.py`; fixture captured `RELIANCE-60d5m.json.gz` (or any liquid name) via `capture.py`.

**Interfaces:**
- Consumes: `nse/plan.json`.
- Produces: `nse/timemachine.json` upstream shape plus `"avg_r_net"`; `simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy")` unchanged signature; session = 75 five-minute slots 09:15-15:30, replay ends at plan `flat_by`.

- [ ] **Step 1: Failing tests:** sessions split by IST date; a session with < 75 bars (special/Muhurat or data gap) excluded from pool and history; seed 19 on fixture reproduces `expected.json` odds exactly; `avg_r_net < avg_r` whenever charges > 0; plan `flat_by 15:10` stops replay at slot 71 (slot 0 = 09:15, slot 71 = 15:10 bar start).
- [ ] **Step 2-4:** Run fail, port (slot count, IST split, net R via charges block), run pass.
- [ ] **Step 5:** Commit `feat(timemachine): NSE session replay net of charges`.

### Task 7: nse-budget

**Files:** Modify `nse-budget/size.py`, `config.example.json`, `SKILL.md`; delete `targets.example.json` (13F weights mode is out of scope: YAGNI); Test `tests/test_budget.py`.

**Interfaces:**
- Consumes: `nse/plan.json`, `nse/prices.json` (Task 10), config.
- Produces: `size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None) -> dict` with `qty, bound_by in {"max loss","position cap","max order","cash"}, max_loss (incl. charges), charges`; writes `nse/orders.json`:
  ```json
  {"created_by":"nse-budget","paper":true,"orders":[{
    "group":"DEMO-2026-10-27-1",
    "legs":[
      {"role":"entry","exchange":"NSE","tradingsymbol":"DEMO","transaction_type":"BUY","quantity":N,"product":"MIS","order_type":"MARKET","variety":"regular","validity":"DAY"},
      {"role":"stop","exchange":"NSE","tradingsymbol":"DEMO","transaction_type":"SELL","quantity":N,"product":"MIS","order_type":"SL-M","trigger_price":1005.20,"variety":"regular","validity":"DAY"},
      {"role":"target","exchange":"NSE","tradingsymbol":"DEMO","transaction_type":"SELL","quantity":N,"product":"MIS","order_type":"LIMIT","price":1034.60,"variety":"regular","validity":"DAY"}],
    "why":"...","plan":{...upstream plan keys..., "charges":..., "max_loss":...}}]}
  ```
  Leg field names are Kite Connect `place_order` params; verify against `zerodha/kite-mcp-server` README in Task 9 and fix here if they differ.

- [ ] **Step 1: Failing tests:** DEMO plan, budget 1,00,000, max loss ₹1000: assert `qty*9.80 + round_trip(qty,1015,1005.20) <= 1000` and `(qty+1)` violates, `bound_by "max loss"`; `max_order_inr 50000` binds at `floor(50000/1015)=49`, `bound_by "max order"`; stop too far (risk ₹1,200/share, max loss ₹1000) -> qty 0, no order written, refusal "a stop ₹1,200.00 away needs max loss ≥ ₹<1200 + round_trip(1,...)>" (plain refusal, never a smaller order); never fractional; `orders.json` legs share qty and `group`.
- [ ] **Step 2-4:** fail, implement (loop qty down from floor(max_loss/risk) until net fits), pass.
- [ ] **Step 5:** Commit `feat(budget): size net of charges, Kite three-leg order`.

### Task 8: nse-risk

**Files:** Modify `nse-risk/check.py`, `SKILL.md`; Test `tests/test_risk.py`; fixture `sec_list.csv` (trimmed, includes a 5% band stock and a BE stock).

**Interfaces:**
- Consumes: `nse/orders.json` (Task 7), config, `nse/portfolio.json`, `nse/prices.json`, `nse/state/broker.json`, `nse/state/bands.json` (cache of `sec_list.csv`, refreshed daily from `https://nsearchives.nseindia.com/content/equities/sec_list.csv`).
- Produces: `check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None, bands=None) -> dict` (upstream result shape, verdict per `group`); `nse/state/risk.json` keyed by `fingerprint(group)` (sha256 of canonical JSON of legs).

Rules: keep upstream format/size/position/cash/duplicate/daily-limit/stop/loss/reward:risk/daily-loss/trades/window. Remove `pdt`, OCC/crypto. Add:
- `series`: BLOCK if symbol not in EQ series (from universe or bands file series column).
- `circuit`: BLOCK entry if `entry >= prev_close*(1+band%) - tick*3` or band is 2%/5% and `allow_banded false` (default false).
- `legs`: BLOCK group missing stop leg while `require_stop`, legs qty mismatch, stop leg not `SL-M`/`SL`, product not `MIS` for a day trade, exchange not `NSE`/`BSE`.
- `fno`/`leverage`: BLOCK if `NFO` exchange or required margin > cash unless flags.
- `window`: IST, `no_entries_before/after`, holidays from common block.
- `loss`: uses `max_loss` including charges.

- [ ] **Step 1: Failing tests:** DEMO group at `10:00` -> PASS; at `14:50` -> BLOCK window; on 2026-11-24 -> BLOCK window "Prakash Gurpurb Sri Guru Nanak Dev"; BE stock -> BLOCK series; entry within 3 ticks of upper band -> BLOCK circuit; missing stop leg -> BLOCK legs; fourth trade of day -> BLOCK trades; exit-only group (SELL qty ≤ held) at 15:25 -> PASS with warnings only (getting out never blocked).
- [ ] **Step 2-4:** fail, implement, pass. Exit code 0/1 unchanged.
- [ ] **Step 5:** Commit `feat(risk): NSE window, series, circuit, Kite legs`.

### Task 9: nse-broker (Kite MCP + paper engine)

**Files:** Modify `nse-broker/ticket.py`, `SKILL.md`, `claude_desktop_config.example.json`; Create `nse-broker/paper.py`; Test `tests/test_broker.py`, `tests/test_paper.py`.

**Interfaces:**
- Consumes: `nse/orders.json`, `nse/state/risk.json`.
- Produces:
  - `ticket.py show|approve|decline|result|fill|flatten|log` (upstream verbs). `show` prints each leg in words plus the exact `place_order` JSON per leg. `approve --reply "<user text>"` must match `yes` + symbol + qty (upstream `parse_yes`). After `fill --leg stop|target`, prints the cancel ticket for the sibling leg (`cancel_order` with `variety`, `order_id`).
  - `paper.py run --group G [--offline DIR]`: fills entry at the open of the bar after approval time, then walks 5m bars: stop if `low <= trigger` (fill at trigger, or bar open if gapped below), target if `high >= price`, else square-off at `flat_by` bar close; writes fills to `nse/state/broker.json` and P&L net of charges. `paper.py status` prints open paper positions.
  - `claude_desktop_config.example.json`: Kite MCP hosted (`"url": "https://mcp.kite.trade/mcp"`) and self-hosted (`zerodha/kite-mcp-server`) variants; note to set `place_order`, `modify_order`, `cancel_order` to "Needs approval".
- `config.broker`: `"paper"` (default) routes approved groups to `paper.py`; `"kite"` hands legs to the Kite MCP tool; anything else -> error.

- [ ] **Step 1: Verify Kite interface:** read `https://github.com/zerodha/kite-mcp-server` README; record exact tool names and `place_order` params in `nse-broker/SKILL.md`. If params differ from Task 7's leg keys, fix `size.py` and its tests first.
- [ ] **Step 2: Failing tests:** `show` on DEMO prints 3 legs and `READY` only when risk PASS fingerprint matches today; `approve --reply "yes"` (no symbol) on a 2-group batch refuses; `fill --leg target` prints `cancel_order` for stop leg; paper: synthetic bars where 10:05 bar low 1004.00 -> stop filled at 1005.20, P&L `-(qty*9.80) - charges`; bar opening at 1003.00 below trigger -> fill at 1003.00 (gap); no hit -> square-off at 15:10 bar close; paper never touches network with `--offline`.
- [ ] **Step 3-5:** fail, implement, pass.
- [ ] **Step 6:** Commit `feat(broker): Kite MCP tickets and local paper engine`.

### Task 10: nse-alerts

**Files:** Modify `nse-alerts/alerts.py`, `alerts.example.json`, `SKILL.md`; Test `tests/test_alerts.py`.

**Interfaces:**
- Consumes: `nse/plan.json`, `nse/orders.json`, `nse/state/broker.json`.
- Produces: `nse/prices.json` (upstream shape, `"currency":"INR"`); `rules_from_plan(plan, cfg_flat_by=None) -> list[dict]` (stop, target, flat-by); rule type `opening_bell` rings at 09:15 IST; new rule type `orphan_leg` (added automatically when a group has a filled exit leg and the sibling still open per `broker.json`); `calendar --refresh` fetches `holiday-master?type=trading`, prints CM holidays for the year and the diff against the common block (does not edit code).

Data: drop Stooq (no reliable NSE coverage); Yahoo only, `query1` then `query2` fallback.

- [ ] **Step 1: Failing tests:** each rule rings once per day; `pct_move` uses prev close from Yahoo meta; `opening_bell` at `09:15` on trading day only; orphan-leg rule fires on fixture state; `calendar --refresh --offline` against saved `holiday-master.json` fixture prints zero diffs for 2026.
- [ ] **Step 2-4:** fail, implement, pass.
- [ ] **Step 5:** Commit `feat(alerts): IST bell, orphan-leg alert, calendar refresh`.

### Task 11: nse-news

**Files:** Modify `nse-news/news.py`, `SKILL.md`; Test `tests/test_news.py`; fixtures `google-RELIANCE.xml`, `Online_announcements.xml` (trimmed), `chart-RELIANCE.json`.

**Interfaces:**
- Consumes: `nse/watchlist.json`, `nse/portfolio.json`, `nse/universe.json` (symbol -> company name).
- Produces: same item shape as upstream (`ticker, time (IST), source, title, link, tags`), state `nse/state/news.json`.

Sources:
- Headlines: `https://news.google.com/rss/search?q=<"Company Name"> share&hl=en-IN&gl=IN&ceid=IN:en` (Yahoo `.NS` RSS returns 0 items, checked 2026-10-28).
- Filings (replaces 8-K): `https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml`; item `title` is the company name, `description` contains `SUBJECT: <category>`; match to ticker by normalised name from `universe.json`.
- Move: Yahoo chart `regularMarketPrice` vs `chartPreviousClose`.
Tags: map `SUBJECT` -> tags: `Financial Result` -> earnings; `Board Meeting` -> board; `Acquisition`/`Amalgamation` -> deal; `Resignation`/`Appointment` -> management; `Credit Rating` -> rating; `Analysts/Institutional Investor Meet` -> analyst; `Outcome of Board Meeting` -> board; else `filing`.

- [ ] **Step 1: Failing tests:** fixture UCO Bank announcement maps to `UCOBANK` with tag `analyst`; name normalisation strips `Limited`/`Ltd`/punctuation; duplicate Google items (same title, different source) collapse; second run shows nothing new; `pubDate "28-Oct-2026 21:01:36"` parsed as IST.
- [ ] **Step 2-4:** fail, implement, pass.
- [ ] **Step 5:** Commit `feat(news): Google News India and NSE announcements`.

### Task 12: nse-analyst

**Files:** Modify (renamed) `nse-analyst/results.py`, `SKILL.md`; delete `metrics.example.json` unless reused; Test `tests/test_analyst.py`; fixture `timeseries-RELIANCE.json`, `Financial_Results.xml`.

**Interfaces:**
- Produces: `fetch_series(sym, types) -> dict[type, list[(date, value)]]` from `https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/<SYM>.NS?type=quarterlyTotalRevenue,quarterlyGrossProfit,quarterlyOperatingIncome,quarterlyNetIncome,quarterlyDilutedEPS&period1=..&period2=..`; `analyse(series, quarters=6) -> dict` (upstream result shape: per quarter revenue, margins, net income, EPS, YoY vs same quarter, TTM); `render(...)` in ₹ crore; latest results filing link from NSE `Financial_Results.xml` RSS matched by company name.
- Indian FY labels: quarter ending 2026-06-30 -> `Q1 FY27`.

- [ ] **Step 1: Failing tests:** fixture RELIANCE yields quarters sorted newest first, net income `₹19,407.0 cr` for 2025-03-31 (from probe: `1.9407E11`), FY label `Q4 FY25`; YoY absent when prior-year quarter missing (prints `n/a`, not 0); missing type (banks lack GrossProfit) -> margin `n/a`.
- [ ] **Step 2-4:** fail, implement, pass.
- [ ] **Step 5:** Commit `feat(analyst): quarterly results from Yahoo fundamentals, INR crore, FY labels`.

### Task 13: nse-deals (replaces the Congress-trades tracker)

**Files:** Create from `ptr.py`: `nse-deals/deals.py`, `SKILL.md`; Test `tests/test_deals.py`; fixtures `bulk.csv`, `block.csv` (captured 2026-10-28), `Insider_Trading.xml`, one `IT_*.xml` PIT XBRL.

**Interfaces:**
- Produces: `parse_deals(csv_text, kind: "bulk"|"block") -> list[dict]` (`date iso, symbol, name, client, side BUY|SELL, qty int, price Decimal, value_inr Decimal, kind`); `parse_pit_xbrl(xml) -> list[dict]` (`symbol, person, category (Promoter/Director/KMP...), side, qty, value_inr, from_date, to_date, mode`); `new_since(rows, state, today)` (upstream semantics: each row alerts once, fingerprint on all fields); CLI `deals.py today [--watch] [--min-value-cr 1]`, `deals.py insider [--watch]`, `deals.py client "<name>"` (all deals by a client in local history `nse/state/deals.json`).
- Sources: `https://archives.nseindia.com/content/equities/bulk.csv`, `.../block.csv` (current day only: the tool keeps its own history), `https://nsearchives.nseindia.com/content/RSS/Insider_Trading.xml` -> linked XBRL files. Note in SKILL.md: insider RSS lagged (latest item 2026-05-02 when checked 2026-10-28); if RSS is stale > 7 days, say so and point to nseindia.com Corporate Filings > Insider Trading.

- [ ] **Step 1: Failing tests:** bulk fixture row `AAATECH`, `VAIBHAV RAJENDRA DOSHI`, BUY, 115017 @ 119.65 -> `value_inr 13761784.05`; block fixture STLTECH BUY 601900 @ 887.00; `--watch STLTECH` flags only that row; rerun prints "nothing new"; `--min-value-cr 1` filters out sub-₹1 cr rows; PIT XBRL fixture extracts person category and side.
- [ ] **Step 2-4:** fail, implement, pass.
- [ ] **Step 5:** Commit `feat(deals): bulk, block and insider deals tracker`.

### Task 14: README, end-to-end reel, release

**Files:** Modify `README.md`; Create `tests/test_reel.py`.

**Interfaces:**
- Consumes: every tool, run via `subprocess` with `--offline tests/fixtures/<skill>` and `--now "2026-10-27 HH:MM"` in a temp dir.

- [ ] **Step 1: Failing test `test_reel.py`:** in a temp project dir, run scanner orb `--at 09:40` -> entry check `--now 09:40` -> exit plan `--write` -> timemachine run `--write` -> budget `--write` -> risk (exit 0) -> broker show (contains `READY`) -> paper run -> alerts check; assert each `nse/*.json` exists and the final paper P&L equals `expected.json`'s value. Use one captured real ticker/day (chosen during Task 3 capture; record in `tests/fixtures/reel.json`).
- [ ] **Step 2:** Run, expect FAIL until README/fixture wiring done; fix wiring only (no tool logic here).
- [ ] **Step 3: README:** upstream structure with Indian copy: install (`/plugin marketplace add <owner>/nse-day-trader-skill`), the 11 table, flow diagram with `nse/`, the reel walkthrough using real numbers from `expected.json`, tools list, Fine print: not financial advice; SEBI loss statistics; not SEBI-registered research/advice; paper default (Indian brokers lack paper accounts, so the local engine simulates fills from 5m bars, slippage not modelled); MIS auto square-off; circuits; tick sizes; charges are estimates, check broker contract note; intraday profits are speculative business income (tax, ITR-3), not tax advice; data unofficial (Yahoo, NSE archives), polite fetching; credit to upstream with link.
- [ ] **Step 4:** Run full suite `python3 -m unittest discover -s tests -v`. Expected: all PASS, 0 skipped.
- [ ] **Step 5:** Commit `docs: README and end-to-end reel test`; tag `v0.1.0`.

---

## Self-review notes

- Spec coverage: all 11 upstream skills mapped (Congress trades -> deals; analyst SEC -> Yahoo fundamentals + NSE RSS). Upstream v1 13F weights mode in budget dropped deliberately.
- Open decisions an executor must not guess (ask before Task 0): GitHub owner for the repo; whether to keep upstream git history (default yes, `upstream` remote); default broker charges profile (Zerodha defaults used).
- Verified live 2026-10-28: Yahoo `.NS` chart/spark/fundamentals; `EQUITY_L.csv`; `bulk.csv`/`block.csv`; `sec_list.csv`; NSE RSS `Online_announcements`, `Financial_Results`, `Insider_Trading`; `holiday-master` API; Google News India RSS. Yahoo `.NS` headline RSS empty. Not verified: Kite MCP exact params (Task 9 Step 1), charges rates (Task 2 Step 4).
