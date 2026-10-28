import copy
import datetime
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

check = load("nse-risk", "check")

CFG_PATH = os.path.join(ROOT, "nse-budget", "config.example.json")
DEMO_PATH = os.path.join(FIX, "nse-risk", "orders-demo.json")
SEC_LIST = os.path.join(FIX, "nse-risk", "sec_list.csv")
TODAY = "2026-10-27"                      # a Tuesday
NOW = "2026-10-27 10:00"                  # IST
PREV = {"DEMO": 1000.0}                   # DEMO's previous close: upper band (20%) at 1200
QUOTE_TIME = datetime.datetime(2026, 10, 27, 4, 52, 13, tzinfo=datetime.timezone.utc)   # 10:22:13 IST

with open(SEC_LIST, encoding="utf-8") as _fh:
    BANDS = check.parse_sec_list(_fh.read())


def at(text):
    return check.parse_now(text)


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def demo(**plan_over):
    with open(DEMO_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o["plan"].update(plan_over)
    return o


def leg(o, role):
    return [lg for lg in o["legs"] if lg.get("role") == role][0]


def rename(o, sym):
    o = copy.deepcopy(o)
    o["group"] = o["group"].replace("DEMO", sym)
    for lg in o["legs"]:
        lg["tradingsymbol"] = sym
    return o


def run(orders, c=None, now=NOW, bands=BANDS, prev_close=PREV, **kw):
    if isinstance(orders, dict):
        orders = [orders]
    kw.setdefault("bands_as_of", at(now).astimezone(check.IST).date().isoformat())   # fresh unless a test says not
    return check.check(orders, c if c is not None else cfg(), now=at(now), bands=bands, prev_close=prev_close, **kw)


def placed(sym, side="BUY", day=TODAY, max_loss=500.0, kind="entry", status="placed", legs=None):
    r = {"group": "%s-%s-1" % (sym, day), "symbol": sym, "side": side, "status": status, "date": day,
         "at": day + "T10:05:00+05:30", "legs": legs}
    if max_loss is not None:
        r["max_loss"] = max_loss
    if kind:
        r["kind"] = kind
    return r


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])


class TheDemo(unittest.TestCase):
    def test_the_demo_group_passes_at_10_00(self):
        res = run(demo())
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["warnings"], [])
        self.assertEqual(res["warnings"], [])
        self.assertEqual((r["group"], r["symbol"], r["side"], r["kind"], r["qty"]),
                         ("DEMO-2026-10-27-1", "DEMO", "BUY", "entry", 73))
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["stop"], r["target"]),
                         (1015.0, "plan", 1005.2, 1034.6))
        self.assertEqual((r["risk_per_share"], r["charges"], r["max_loss"], r["reward_risk"]),
                         (9.8, 73.26, 788.66, 2.0))
        self.assertEqual(r["value_inr"], 74095.0)
        self.assertEqual(res["cash_after"], 25905.0)
        self.assertEqual(res["date"], TODAY)
        self.assertEqual(res["now_ist"], "2026-10-27 10:00")
        self.assertIs(res["paper"], True)

    def test_fingerprint_is_the_legs_canonical_json(self):
        o = demo()
        fp = check.fingerprint(o)
        self.assertRegex(fp, r"^[0-9a-f]{64}$")
        shuffled = dict(o, legs=[dict(reversed(list(lg.items()))) for lg in o["legs"]])
        self.assertEqual(check.fingerprint(shuffled), fp)
        self.assertEqual(check.fingerprint(dict(o, why="anything", plan={}, group="X")), fp)
        more = copy.deepcopy(o)
        for lg in more["legs"]:
            lg["quantity"] = 74
        self.assertNotEqual(check.fingerprint(more), fp)
        moved = copy.deepcopy(o)
        leg(moved, "stop")["trigger_price"] = 1005.1
        self.assertNotEqual(check.fingerprint(moved), fp)
        import hashlib
        blob = json.dumps(o["legs"], sort_keys=True, separators=(",", ":"))
        self.assertEqual(fp, hashlib.sha256(blob.encode("utf-8")).hexdigest())

    def test_cli_writes_the_verdicts_keyed_by_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "state", "risk.json")
            none = os.path.join(d, "none")
            prices = os.path.join(d, "prices.json")
            with open(prices, "w") as fh:
                json.dump({"as_of": "2026-10-27T03:50:00Z", "prices": {"DEMO": 1012.0},
                           "detail": {"DEMO": {"prev_close": 1000.0, "time": "2026-10-27T03:50:00Z"}}}, fh)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", DEMO_PATH, "--config", CFG_PATH, "--portfolio", none,
                                   "--prices", prices, "--state", none, "--bands", SEC_LIST, "--universe", none,
                                   "--now", NOW, "--out", out])
            text = buf.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("PASS  #1 DEMO-2026-10-27-1: BUY 73 DEMO, entry", text)
            self.assertIn("entry ₹1,015.00 (the plan's entry), stop ₹1,005.20, target ₹1,034.60: "
                          "risk ₹9.80 a share", text)
            self.assertIn("max loss ₹788.66 with ₹73.26 charges (limit ₹1,000.00), reward:risk 2.00 (min 1)", text)
            self.assertIn("1 passed, 0 blocked at 2026-10-27 10:00 IST", text)
            self.assertIn("not financial advice", text)
            with open(out) as fh:
                rec = json.load(fh)
            fp = check.fingerprint(demo())
            self.assertEqual(rec["date"], TODAY)
            self.assertEqual(rec["results"][0]["fingerprint"], fp)
            self.assertEqual(rec["verdicts"][fp]["verdict"], "PASS")
            self.assertEqual(rec["verdicts"][fp]["date"], TODAY)
            self.assertEqual(rec["verdicts"][fp]["group"], "DEMO-2026-10-27-1")
            self.assertEqual(rec["verdicts"][fp]["max_loss"], 788.66)
            # a later blocked check of another group keeps today's earlier verdicts
            with redirect_stdout(io.StringIO()):
                code = check.main(["--orders", DEMO_PATH, "--config", CFG_PATH, "--portfolio", none,
                                   "--prices", prices, "--state", none, "--bands", SEC_LIST, "--universe", none,
                                   "--now", "2026-10-27 14:50", "--out", out])
            self.assertEqual(code, 1)
            with open(out) as fh:
                rec = json.load(fh)
            self.assertEqual(rec["verdicts"][fp]["verdict"], "BLOCK")
            self.assertEqual(len(rec["verdicts"]), 1)


class Window(unittest.TestCase):
    def test_after_the_entry_window(self):
        res = run(demo(), now="2026-10-27 14:50")
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertEqual(res["results"][0]["reasons"],
                         ["window: no new entries after 14:45 IST (it is 14:50)"])

    def test_on_a_holiday_the_reason_names_it(self):
        res = run(demo(), now="2026-11-24 10:00")
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("window: NSE is closed today (Prakash Gurpurb Sri Guru Nanak Dev, Tue 2026-11-24 10:00 IST)",
                      reasons(res))

    def test_the_window_cases(self):
        cases = [("2026-10-27 09:25", "no new entries before 09:30 IST (it is 09:25)"),
                 ("2026-10-27 09:05", "NSE is closed (pre-open session until 09:15, Tue 2026-10-27 09:05 IST"),
                 ("2026-10-27 08:00", "NSE is closed (before the open, Tue 2026-10-27 08:00 IST"),
                 ("2026-10-27 15:35", "NSE is closed (after the close"),
                 ("2026-10-31 10:00", "NSE is closed today (weekend"),
                 ("2026-11-10 10:00", "NSE is closed today (Diwali-Balipratipada"),
                 ("2026-10-27T09:20:00Z", "no new entries after 14:45 IST (it is 14:50)")]
        for now, want in cases:
            self.assertIn("window: " + want, reasons(run(demo(), now=now)), now)
        for now in ("2026-10-27 09:30", "2026-10-27 14:45", "2026-10-27T04:30:00Z"):
            self.assertEqual(verdicts(run(demo(), now=now)), ["PASS"], now)

    def test_a_special_session_counts_as_open_within_its_times(self):
        c = cfg(special_sessions={"2026-11-08": ["18:00", "19:00"]})
        res = run(demo(), c, now="2026-11-08 18:15")
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("special session", " ".join(res["warnings"]))
        self.assertIn("NSE is closed (after the close", reasons(run(demo(), c, now="2026-11-08 19:05")))
        self.assertIn("weekend", reasons(run(demo(), now="2026-11-08 18:15")))

    def test_a_year_outside_the_calendar_warns(self):
        res = run(demo(), now="2027-01-05 10:00")
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("NSE calendar for 2027 not loaded: run nse-alerts/alerts.py calendar --refresh or check "
                      "nseindia.com (treating Mon-Fri as open)", res["warnings"])

    def test_the_window_can_be_switched_off_and_must_be_hh_mm(self):
        res = run(demo(), cfg(no_entries_before=None, no_entries_after=None), now="2026-10-31 10:00")
        self.assertEqual(verdicts(res), ["PASS"])
        with self.assertRaises(check.RiskError):
            run(demo(), cfg(no_entries_before="9.30am"))


class SeriesAndCircuit(unittest.TestCase):
    def test_parse_sec_list(self):
        self.assertEqual(BANDS["DEMO"], {"series": "EQ", "band": 20.0, "name": "DEMO SYNTHETIC TEST STOCK",
                                         "remarks": "-"})
        self.assertEqual(BANDS["3IINFOLTD"]["series"], "BE")
        self.assertEqual(BANDS["21STCENMGM"]["band"], 5.0)
        self.assertIsNone(BANDS["RELIANCE"]["band"])
        self.assertEqual(BANDS["M&M"]["series"], "EQ")

    def test_a_be_stock_is_blocked(self):
        for sym in ("3IINFOLTD", "ABAN", "AGSTRA", "AAKAAR"):
            res = run(rename(demo(), sym), c=cfg(allow_banded=True), prev_close={sym: 900.0})
            self.assertEqual(verdicts(res), ["BLOCK"], sym)
            self.assertIn("series: %s trades in the %s series, not EQ" % (sym, BANDS[sym]["series"]), reasons(res))

    def test_the_universe_knows_eq_symbols(self):
        o = rename(demo(), "NEWCO")
        res = run(o, prev_close={"NEWCO": 1000.0}, universe=["NEWCO", "RELIANCE"])
        self.assertNotIn("series", warnings(res) + reasons(res))
        # a symbol in neither loaded list is blocked
        res = run(o, prev_close={"NEWCO": 1000.0})
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("series: NEWCO is not in the EQ universe; refresh bands or universe", reasons(res))
        res = run(o, bands=None, prev_close={"NEWCO": 1000.0}, universe=["RELIANCE"])
        self.assertIn("series: NEWCO is not in the EQ universe; refresh bands or universe", reasons(res))

    def test_a_be_stock_with_no_bands_is_blocked_by_the_universe(self):
        res = run(rename(demo(), "ABAN"), bands=None, prev_close={"ABAN": 1000.0}, universe=["DEMO", "RELIANCE"])
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("series: ABAN is not in the EQ universe; refresh bands or universe", reasons(res))

    def test_series_is_only_a_warning_when_neither_list_is_loaded(self):
        res = run(rename(demo(), "NEWCO"), bands=None, prev_close={"NEWCO": 1000.0})
        self.assertIn("series unknown", warnings(res))
        self.assertNotIn("series", reasons(res))

    def test_an_entry_within_three_ticks_of_the_upper_band(self):
        # 846 x 1.20 = 1015.20; three ticks of 0.10 below it is 1014.90; the entry 1015 is at or above it
        res = run(demo(), prev_close={"DEMO": 846.0})
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("circuit: the entry ₹1,015.00 is within 3 ticks of DEMO's upper band ₹1,015.20 "
                      "(previous close ₹846.00 + 20%)", reasons(res))
        self.assertEqual(verdicts(run(demo(), prev_close={"DEMO": 846.1})), ["PASS"])

    def test_a_2_or_5_percent_band_is_blocked_unless_allowed(self):
        o = rename(demo(), "21STCENMGM")
        res = run(o, prev_close={"21STCENMGM": 1000.0})
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("circuit: 21STCENMGM has a 5% price band", reasons(res))
        res = run(o, cfg(allow_banded=True), prev_close={"21STCENMGM": 966.8})
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("within 3 ticks of 21STCENMGM's upper band ₹1,015.14 (previous close ₹966.80 + 5%)", reasons(res))
        res = run(o, cfg(allow_banded=True), prev_close={"21STCENMGM": 980.0})
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))

    def test_no_previous_close_blocks_an_entry(self):
        res = run(demo(), prev_close={})
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("circuit: no previous close from today for DEMO", reasons(res))
        self.assertIn("nse-alerts/alerts.py quote DEMO", reasons(res))
        res = run(demo(), prev_close=None)
        self.assertIn("nse-alerts/alerts.py quote DEMO", reasons(res))
        res = run(rename(demo(), "RELIANCE"), prev_close={})
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))   # no fixed band: nothing to compare against
        self.assertEqual(res["results"][0]["warnings"], [])
        self.assertIn("no fixed price band", " ".join(res["results"][0]["info"]))

    def test_missing_or_stale_bands_block_an_entry(self):
        res = run(demo(), bands=None)
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("circuit: NSE price bands are not loaded", reasons(res))
        self.assertIn("run check.py --refresh-bands", reasons(res))
        res = run(demo(), bands_as_of="2026-10-26")
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("circuit: the NSE price bands are from 2026-10-26, before today (2026-10-27): run check.py "
                      "--refresh-bands", reasons(res))
        for fresh in (TODAY, "2026-10-28"):
            self.assertEqual(verdicts(run(demo(), bands_as_of=fresh)), ["PASS"], fresh)
        for bad in (None, "", "27/10/2026", "2026-13-40", "yesterday", 20261027):
            res = run(demo(), bands_as_of=bad)
            self.assertEqual(verdicts(res), ["BLOCK"], bad)
            self.assertIn("no valid as_of date", reasons(res))
            self.assertIn("run check.py --refresh-bands", reasons(res))

    def test_a_symbol_missing_from_fresh_bands_is_a_warning(self):
        res = run(rename(demo(), "NEWCO"), prev_close={"NEWCO": 1000.0}, universe=["NEWCO"], bands_as_of=TODAY)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("circuit: no price band for NEWCO", warnings(res))

    def test_an_exit_is_never_blocked_by_bands_or_prev_close(self):
        pf = {"cash": 25905.0, "positions": [{"symbol": "DEMO", "qty": 73}]}
        o = demo()
        o["legs"] = [lg for lg in o["legs"] if lg["role"] != "entry"]
        for kw in ({"bands": None}, {"bands_as_of": "2026-10-01"}, {"bands_as_of": None},
                   {"bands_as_of": "not-a-date"}, {"prev_close": {}}):
            res = run(o, portfolio=pf, **kw)
            self.assertEqual((verdicts(res), res["results"][0]["kind"]), (["PASS"], "exit"), kw)


class Legs(unittest.TestCase):
    def test_a_missing_stop_leg_is_blocked(self):
        o = demo()
        o["legs"] = [lg for lg in o["legs"] if lg["role"] != "stop"]
        res = run(o)
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("legs: no stop leg", reasons(res))
        res = run(o, cfg(require_stop=False))
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("in the plan only", warnings(res))

    def test_leg_rules(self):
        def bad(change, want, c=None):
            o = demo()
            change(o)
            res = run(o, c)
            self.assertEqual(verdicts(res), ["BLOCK"], want)
            self.assertIn(want, reasons(res))

        bad(lambda o: leg(o, "target").update(quantity=70), "legs: the legs' quantities differ (73, 73, 70)")
        bad(lambda o: leg(o, "stop").update(order_type="LIMIT", price=1005.2),
            "legs: the stop leg must be SL-M or SL, not LIMIT")
        bad(lambda o: [lg.update(product="CNC") for lg in o["legs"]], "legs: product CNC on the entry leg")
        bad(lambda o: [lg.update(exchange="MCX") for lg in o["legs"]], "legs: exchange MCX")
        bad(lambda o: leg(o, "stop").update(trigger_price=1016.0),
            "legs: the stop trigger ₹1,016.00 must be below the entry ₹1,015.00")
        bad(lambda o: leg(o, "target").update(price=1014.0),
            "legs: the target ₹1,014.00 must be above the entry ₹1,015.00")
        bad(lambda o: leg(o, "entry").update(order_type="BRACKET"), "format: unknown order_type 'BRACKET'")
        bad(lambda o: leg(o, "target").update(tradingsymbol="OTHER"), "format: every leg must be the same symbol")
        bad(lambda o: leg(o, "entry").update(quantity=7.5), "format: quantity must be a whole number")
        bad(lambda o: leg(o, "stop").pop("trigger_price"), "format: an SL-M leg needs a trigger_price")
        bad(lambda o: o.update(legs=[]), "format: no legs")

    def test_an_sl_stop_and_a_limit_entry(self):
        o = demo()
        leg(o, "stop").update(order_type="SL", price=1004.0)
        leg(o, "entry").update(order_type="LIMIT", price=1014.0)
        res = run(o, prices={"DEMO": 1020.0})
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["entry_ref"], r["entry_ref_from"]), ("PASS", 1014.0, "limit"), r["reasons"])

    def test_fno_and_leverage(self):
        o = demo()
        for lg in o["legs"]:
            lg["exchange"] = "NFO"
        self.assertIn("fno: NFO", reasons(run(o)))
        self.assertNotIn("fno:", reasons(run(o, cfg(allow_fno=True))))
        pf = {"cash": 50000, "positions": []}
        res = run(demo(), cfg(cash_buffer_pct=0, max_position_pct=100), portfolio=pf)
        self.assertIn("leverage: needs ₹74,095.00 but only ₹50,000.00 cash", reasons(res))
        res = run(demo(), cfg(cash_buffer_pct=0, max_position_pct=500, allow_leverage=True), portfolio=pf)
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("leverage", warnings(res))


class LossAndLimits(unittest.TestCase):
    def test_the_loss_includes_charges(self):
        res = run(demo(), cfg(max_loss_per_trade_inr=780))
        self.assertEqual(res["results"][0]["reasons"],
                         ["loss: 73 shares x ₹9.80 risk a share = ₹715.40 plus ₹73.26 charges = ₹788.66 at the "
                          "stop, over your ₹780.00 max loss per trade"])
        self.assertEqual(verdicts(run(demo(), cfg(max_loss_per_trade_inr=789))), ["PASS"])

    def test_reward_risk(self):
        o = demo()
        leg(o, "target").update(price=1020.0)
        res = run(o)
        self.assertIn("reward:risk: 0.51 (target ₹1,020.00, entry ₹1,015.00, stop ₹1,005.20) is under your minimum 1",
                      reasons(res))
        self.assertEqual(verdicts(run(o, cfg(min_reward_risk=0.5))), ["PASS"])

    def test_a_fresh_higher_price_is_the_entry(self):
        res = run(demo(), now="2026-10-27 10:30", prices={"DEMO": 1017.0}, price_times={"DEMO": QUOTE_TIME})
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (1017.0, "price", 11.8))
        self.assertIn("above the plan's entry", " ".join(r["info"]))

    def test_a_stale_or_lower_price_is_not_used(self):
        res = run(demo(), now="2026-10-27 11:00", prices={"DEMO": 1017.0}, price_times={"DEMO": QUOTE_TIME})
        self.assertEqual(res["results"][0]["entry_ref"], 1015.0)
        self.assertIn("older than 15 minutes", warnings(res))
        res = run(demo(), prices={"DEMO": 1010.0})
        self.assertEqual(res["results"][0]["entry_ref"], 1015.0)

    def test_a_fourth_trade_of_the_day_is_blocked(self):
        three = {"orders": [placed("AAA"), placed("BBB"), placed("CCC")]}
        res = run(demo(), state=three)
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertEqual(res["results"][0]["reasons"],
                         ["trades: 3 entries placed today plus 0 passed earlier in this batch, your limit is 3 a day"])
        two = {"orders": [placed("AAA"), placed("BBB")]}
        res = run(demo(), state=two)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("today before this: 2 of 3 trades, ₹1,000.00 of ₹3,000.00 daily loss",
                      res["results"][0]["info"][-1])
        yesterday = {"orders": [placed(s, day="2026-10-26") for s in ("AAA", "BBB", "CCC")]}
        self.assertEqual(verdicts(run(demo(), state=yesterday)), ["PASS"])

    def test_daily_loss(self):
        two = {"orders": [placed("AAA", max_loss=1200), placed("BBB", max_loss=1100)]}
        res = run(demo(), state=two)
        self.assertIn("daily loss: ₹2,300.00 already at risk today plus ₹788.66 for this one = ₹3,088.66, over your "
                      "₹3,000.00 daily loss limit", reasons(res))

    def test_broker_state_keyed_by_fingerprint(self):
        st = {"orders": {"fp1": placed("AAA"), "fp2": placed("BBB"), "fp3": placed("CCC")}}
        self.assertIn("trades: 3 entries placed today", reasons(run(demo(), state=st)))

    def test_exits_and_declines_do_not_count_as_trades(self):
        st = {"orders": [placed("AAA"), placed("AAA", side="SELL", kind="exit", max_loss=None),
                         placed("BBB", status="declined"), placed("CCC", status="failed")]}
        res = run(demo(), state=st)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual(res["today"]["entries_placed"], 1)

    def test_the_batch_counts_toward_the_limits(self):
        orders = [rename(demo(), s) for s in ("AAA", "BBB", "CCC", "DDD")]
        prev = {s: 1000.0 for s in ("AAA", "BBB", "CCC", "DDD")}
        c = cfg(budget=10000000, max_order_inr=10000000)
        res = run(orders, c, prev_close=prev, universe=list(prev))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "PASS", "BLOCK"])
        why = reasons(res, 3)
        self.assertIn("trades: 0 entries placed today plus 3 passed earlier", why)
        self.assertIn("daily loss: ₹2,365.98 already at risk today plus ₹788.66 for this one = ₹3,154.64", why)
        self.assertIn("daily limit: 0 orders placed today plus 9 passed earlier in this batch, this group is 3 more", why)

    def test_basics_blocked_size_position_cash_duplicate(self):
        self.assertIn("blocked: DEMO is on your blocked list", reasons(run(demo(), cfg(blocked=["demo"]))))
        self.assertIn("size: ₹74,095.00 is over your ₹50,000.00 max order", reasons(run(demo(), cfg(max_order_inr=50000))))
        self.assertIn("size: ₹74,095.00 is under your ₹80,000.00 minimum order",
                      reasons(run(demo(), cfg(min_order_inr=80000))))
        self.assertIn("position: DEMO would be ₹74,095.00", reasons(run(demo(), cfg(max_position_pct=50))))
        self.assertIn("cash: leaves ₹25,905.00 cash, under your 30% buffer of ₹30,000.00",
                      reasons(run(demo(), cfg(cash_buffer_pct=30))))
        res = run([demo(), demo()])
        self.assertIn("duplicate: a second BUY DEMO in this batch", reasons(res, 1))
        res = run(demo(), state={"orders": [placed("DEMO")]})
        self.assertIn("duplicate: a BUY DEMO was already placed today", reasons(res))
        st = {"orders": [placed("T%d" % i, kind="exit", max_loss=None, legs=1) for i in range(8)]}
        self.assertIn("daily limit: 8 orders placed today", reasons(run(demo(), state=st)))

    # ------------------------------------------------ Gap 2: an approved group not yet placed is an open entry
    def approved(self, sym, fp=None, max_loss=500.0, legs=3, value=None, status="approved", at="10:05"):
        r = {"group": "%s-%s-1" % (sym, TODAY), "fingerprint": fp or "fp-" + sym, "symbol": sym, "side": "BUY",
             "qty": 20, "kind": "entry", "status": status, "date": TODAY, "at": "%sT%s:00+05:30" % (TODAY, at),
             "max_loss": max_loss, "legs": legs}
        if value is not None:
            r["value_inr"] = value
        return r

    def test_an_approval_counts_as_a_trade(self):
        res = run(demo(), cfg(max_trades_per_day=1), state={"orders": [self.approved("AAA")]})
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("trades: 1 entries placed today plus 0 passed earlier in this batch, your limit is 1 a day",
                      reasons(res))
        self.assertEqual(res["today"]["entries_placed"], 1)
        self.assertEqual(res["today"]["approved_open"], 1)
        self.assertIn("approved", " ".join(res["warnings"]))

    def test_a_declined_or_refused_approval_does_not_count(self):
        for st in ("declined", "denied", "failed"):
            recs = [self.approved("AAA"), dict(self.approved("AAA", at="10:06"), status=st)]
            res = run(demo(), cfg(max_trades_per_day=1), state={"orders": recs})
            self.assertEqual(verdicts(res), ["PASS"], st)
            self.assertEqual(res["today"]["entries_placed"], 0, st)
            self.assertNotIn("approved_open", res["today"], st)

    def test_an_approval_then_its_placement_counts_once(self):
        a = self.approved("AAA")
        p = dict(placed("AAA", max_loss=500.0), fingerprint=a["fingerprint"], at=TODAY + "T10:06:00+05:30")
        res = run(demo(), cfg(max_trades_per_day=2), state={"orders": [a, p]})
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual(res["today"]["entries_placed"], 1)
        self.assertEqual(res["today"]["loss_at_risk"], 500.0)
        self.assertEqual(res["today"]["orders_placed"], 1)
        res = run(demo(), cfg(max_trades_per_day=1), state={"orders": [a, p]})
        self.assertIn("trades: 1 entries placed today", reasons(res))

    def test_the_daily_loss_sums_an_approvals_max_loss(self):
        st = {"orders": [placed("AAA", max_loss=1200), self.approved("BBB", max_loss=1100)]}
        res = run(demo(), state=st)
        self.assertEqual(res["today"]["loss_at_risk"], 2300.0)
        self.assertIn("daily loss: ₹2,300.00 already at risk today plus ₹788.66 for this one = ₹3,088.66, over your "
                      "₹3,000.00 daily loss limit", reasons(res))

    def test_an_approval_counts_its_legs_toward_the_order_limit(self):
        st = {"orders": [self.approved("AAA", legs=3), self.approved("BBB", legs=3)]}
        res = run(demo(), cfg(max_orders_per_day=8), state=st)
        self.assertEqual(res["today"]["orders_placed"], 6)
        self.assertIn("daily limit: 6 orders placed today plus 0 passed earlier in this batch, this group is 3 more",
                      reasons(res))

    def test_an_approval_is_a_duplicate(self):
        res = run(demo(), state={"orders": [self.approved("DEMO")]})
        self.assertIn("duplicate: a BUY DEMO was already placed today", reasons(res))

    def test_an_approval_holds_its_cash_and_position(self):
        # 100,000 budget, 30% buffer: DEMO alone leaves 25,905 (blocked); with 20,000 already approved
        # elsewhere, a 20% buffer blocks too
        st = {"orders": [self.approved("AAA", value=20000.0)]}
        self.assertEqual(verdicts(run(demo(), cfg(cash_buffer_pct=20))), ["PASS"])
        res = run(demo(), cfg(cash_buffer_pct=20), state=st)
        self.assertIn("cash: leaves ₹5,905.00 cash", reasons(res))
        st = {"orders": [self.approved("DEMO", value=20000.0, fp="fp-other-demo")]}
        res = run(demo(), cfg(max_position_pct=90), state=st)
        self.assertIn("position: DEMO would be ₹94,095.00", reasons(res))

    def test_a_filled_approval_no_longer_holds_cash(self):
        # the fill moved it into the portfolio: holding it again would count the cash twice
        a = self.approved("AAA", value=20000.0)
        f = {"status": "filled", "leg": "entry", "fingerprint": a["fingerprint"], "symbol": "AAA", "side": "BUY",
             "qty": 20, "price": 1000.0, "date": TODAY, "at": TODAY + "T10:07:00+05:30"}
        self.assertEqual(verdicts(run(demo(), cfg(cash_buffer_pct=20), state={"orders": [a, f]})), ["PASS"])

    def test_an_approval_from_yesterday_does_not_count(self):
        y = dict(self.approved("AAA"), date="2026-10-26", at="2026-10-26T10:05:00+05:30")
        res = run(demo(), cfg(max_trades_per_day=1), state={"orders": [y]})
        self.assertEqual(verdicts(res), ["PASS"])

    def test_live_account_warns(self):
        # I5: LIVE is "broker": "kite" (with "paper": false to match)
        res = run(demo(), cfg(broker="kite", paper=False))
        self.assertIn("LIVE", res["warnings"][0])
        self.assertIs(res["paper"], False)
        res = run(demo(), {"budget": 100000, "broker": "kite"})
        self.assertIn("LIVE", res["warnings"][0])
        self.assertIs(res["paper"], False)

    def test_broker_and_paper_that_disagree_are_a_config_error(self):
        for over in ({"broker": "kite", "paper": True}, {"broker": "paper", "paper": False}, {"paper": False}):
            with self.assertRaises(check.RiskError) as cm:
                run(demo(), cfg(**over) if "broker" in over else dict(cfg(), **over))
            self.assertIn('"broker"', str(cm.exception))
            self.assertIn('"paper"', str(cm.exception))
            with tempfile.TemporaryDirectory() as d:
                cp = os.path.join(d, "config.json")
                with open(cp, "w") as fh:
                    json.dump(dict(cfg(), **over), fh)
                err = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(err):
                    code = check.main(["--orders", DEMO_PATH, "--config", cp, "--now", NOW, "--no-write",
                                       "--state", os.path.join(d, "none"), "--bands", SEC_LIST])
                self.assertEqual(code, 1, over)
                self.assertIn('"broker"', err.getvalue())
                self.assertIn('"paper"', err.getvalue())


class Exits(unittest.TestCase):
    PF = {"cash": 25905.0, "positions": [{"symbol": "DEMO", "qty": 73}]}

    def sell(self, qty=73, otype="MARKET", **extra):
        lg = {"role": "exit", "exchange": "NSE", "tradingsymbol": "DEMO", "transaction_type": "SELL",
              "quantity": qty, "product": "MIS", "order_type": otype, "variety": "regular", "validity": "DAY"}
        lg.update(extra)
        return {"group": "DEMO-2026-10-27-flat", "legs": [lg]}

    def test_getting_out_at_15_25_is_never_blocked(self):
        full = {"orders": [placed("DEMO"), placed("BBB"), placed("CCC")] +
                          [placed("DEMO", side="SELL", kind="exit", max_loss=None, legs=1)]}
        c = cfg(blocked=["DEMO"], max_order_inr=100, max_daily_loss_inr=50, max_orders_per_day=4)
        res = run(self.sell(), c, now="2026-10-27 15:25", portfolio=self.PF, state=full,
                  prev_close={"DEMO": 846.0}, prices={"DEMO": 1030.0})
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])
        self.assertEqual(r["reasons"], [])
        why = warnings(res)
        for word in ("blocked:", "size:", "duplicate:", "daily limit:", "window:"):
            self.assertIn("exit, not blocked: " + word, why)
        self.assertNotIn("trades:", why)
        res = run(self.sell(), now="2026-10-31 12:00", portfolio=self.PF)
        self.assertEqual(verdicts(res), ["PASS"])

    def test_a_protective_stop_and_target_for_a_holding_is_an_exit(self):
        o = demo()
        o["legs"] = [lg for lg in o["legs"] if lg["role"] != "entry"]
        res = run(o, now="2026-10-27 15:00", portfolio=self.PF)
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])

    def test_format_series_and_short_still_apply(self):
        res = run(self.sell(otype="LIMIT"), portfolio=self.PF)
        self.assertIn("format: a LIMIT leg needs a price", reasons(res))
        res = run(self.sell(qty=80), portfolio=self.PF)
        self.assertIn("short: selling more DEMO than you hold (73 shares)", reasons(res))
        res = run(self.sell())
        self.assertIn("record the fill first", reasons(res))
        self.assertNotIn("short:", reasons(run(self.sell(qty=80), cfg(allow_short=True), portfolio=self.PF)))
        pf = {"cash": 1000, "positions": [{"symbol": "ABAN", "qty": 10}]}
        o = rename(self.sell(qty=10), "ABAN")
        self.assertIn("series: ABAN trades in the BE series", reasons(run(o, portfolio=pf)))

    def test_buy_then_sell_in_one_batch(self):
        res = run([demo(), self.sell()])
        self.assertEqual(verdicts(res), ["PASS", "PASS"], reasons(res, 1))
        self.assertEqual(res["results"][1]["kind"], "exit")


class Verdicts(unittest.TestCase):
    def test_write_verdicts_records_the_run_time_at_the_top_and_on_its_verdicts(self):
        # I1: nse-broker trusts a verdict only when its checked_at is the latest run's checked_at
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "risk.json")
            a, b = demo(), rename(demo(), "BBB")
            check.write_verdicts(out, run(a), "orders.json")
            with open(out) as fh:
                first = json.load(fh)
            fa = check.fingerprint(a)
            self.assertEqual(first["checked_at"], first["verdicts"][fa]["checked_at"])
            for v in first["verdicts"].values():
                v["checked_at"] = "2026-10-27T04:00:00Z"
            first["checked_at"] = "2026-10-27T04:00:00Z"
            with open(out, "w") as fh:
                json.dump(first, fh)
            check.write_verdicts(out, run(b), "orders.json")
            with open(out) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["verdicts"][check.fingerprint(b)]["checked_at"], doc["checked_at"])
            self.assertEqual(doc["verdicts"][fa]["checked_at"], "2026-10-27T04:00:00Z")
            self.assertNotEqual(doc["checked_at"], "2026-10-27T04:00:00Z")


class Cli(unittest.TestCase):
    def base(self, d, *extra):
        """CLI args; a prices.json with DEMO's previous close quoted on 2026-10-27 (an explicit --prices wins)."""
        none = os.path.join(d, "none")
        pp = os.path.join(d, "prev-close.json")
        with open(pp, "w") as fh:
            json.dump({"as_of": "2026-10-27T04:00:00Z", "prices": {},
                       "detail": {"DEMO": {"prev_close": 1000.0, "time": "2026-10-27T04:00:00Z"}}}, fh)
        return ["--orders", DEMO_PATH, "--config", CFG_PATH, "--portfolio", none, "--state", none,
                "--universe", none, "--prices", pp, "--no-write"] + list(extra)

    def test_prices_file_freshness_and_prev_close(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump({"as_of": "2026-10-27T04:55:00Z", "prices": {"DEMO": 1017.0},
                           "detail": {"DEMO": {"prev_close": 1000.0, "time": "2026-10-27T04:52:13Z",
                                               "fetched": "2026-10-27T04:55:00Z", "source": "yahoo"}}}, fh)
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(check.main(self.base(d, "--prices", pp, "--bands", SEC_LIST, "--now",
                                                      "2026-10-27 10:30", "--json")), 0)
            r = json.loads(buf.getvalue())["results"][0]
            self.assertEqual((r["entry_ref"], r["entry_ref_from"]), (1017.0, "price"))
            self.assertEqual(r["warnings"], [])
            with open(pp, "w") as fh:
                json.dump({"as_of": "2026-10-27T04:55:00Z", "prices": {"DEMO": 1017.0},
                           "detail": {"DEMO": {"prev_close": 847.5, "time": "2026-10-27T04:52:13Z"}}}, fh)
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(check.main(self.base(d, "--prices", pp, "--bands", SEC_LIST, "--now",
                                                      "2026-10-27 10:30")), 1)
            self.assertIn("BLOCK #1", buf.getvalue())
            self.assertIn("circuit:", buf.getvalue())

    def test_refresh_bands_offline_writes_the_cache(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "state", "bands.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(self.base(d, "--bands", cache, "--refresh-bands", "--offline",
                                            os.path.dirname(SEC_LIST), "--now", NOW))
            self.assertEqual(code, 0, buf.getvalue())
            with open(cache) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["as_of"], TODAY)
            self.assertEqual(doc["bands"]["DEMO"]["band"], 20.0)
            self.assertEqual(doc["bands"]["ABAN"]["series"], "BE")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(self.base(d, "--bands", cache, "--now", "2026-10-28 10:00", "--offline", d,
                                            "--json"))
            doc = json.loads(buf.getvalue())
            self.assertEqual(code, 1)
            self.assertIn("price bands are from 2026-10-27", " ".join(doc["warnings"]))
            self.assertIn("run check.py --refresh-bands", " | ".join(doc["results"][0]["reasons"]))
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(self.base(d, "--bands", os.path.join(d, "nobands.json"), "--now", NOW,
                                            "--offline", d, "--json"))
            doc = json.loads(buf.getvalue())
            self.assertEqual(code, 1)
            self.assertIn("--refresh-bands", " ".join(doc["warnings"]))
            self.assertIn("circuit: NSE price bands are not loaded", " | ".join(doc["results"][0]["reasons"]))

    def test_missing_bands_try_one_live_refresh(self):
        with open(SEC_LIST, encoding="utf-8") as fh:
            text = fh.read()
        calls = []
        real = check.fetch_sec_list

        def fake(offline=None):
            calls.append(offline)
            return text, check.SEC_LIST_URL

        def down(offline=None):
            calls.append(offline)
            raise check.RiskError("could not fetch %s: network down" % check.SEC_LIST_URL)
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "state", "bands.json")
            try:
                check.fetch_sec_list = fake
                with redirect_stdout(io.StringIO()):
                    code = check.main(self.base(d, "--bands", cache, "--now", NOW))
                self.assertEqual((code, calls), (0, [None]))
                with open(cache) as fh:
                    self.assertEqual(json.load(fh)["as_of"], TODAY)
                check.fetch_sec_list = down
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = check.main(self.base(d, "--bands", cache, "--now", "2026-10-28 10:00", "--json"))
                doc = json.loads(buf.getvalue())
                self.assertEqual((code, len(calls)), (1, 2))
                self.assertIn("could not refresh the price bands", " ".join(doc["warnings"]))
                self.assertIn("run check.py --refresh-bands", " | ".join(doc["results"][0]["reasons"]))
                # an exit-only file never fetches and is never blocked
                ex = os.path.join(d, "exit.json")
                with open(ex, "w") as fh:
                    json.dump({"orders": [Exits().sell()]}, fh)
                pf = os.path.join(d, "pf.json")
                with open(pf, "w") as fh:
                    json.dump(Exits.PF, fh)
                args = self.base(d, "--bands", os.path.join(d, "nobands.json"), "--now", NOW)
                args[args.index("--orders") + 1] = ex
                args[args.index("--portfolio") + 1] = pf
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(check.main(args), 0)
                self.assertEqual(len(calls), 2)
            finally:
                check.fetch_sec_list = real

    def test_a_bands_json_with_no_or_a_bad_as_of_is_stale(self):
        for as_of in (None, "27-10-2026", "2026-02-30", 5):
            with tempfile.TemporaryDirectory() as d:
                cache = os.path.join(d, "bands.json")
                doc = {"bands": BANDS}
                if as_of is not None:
                    doc["as_of"] = as_of
                with open(cache, "w") as fh:
                    json.dump(doc, fh)
                self.assertIsNone(check.load_bands(cache)[1], as_of)
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = check.main(self.base(d, "--bands", cache, "--now", NOW, "--offline", d, "--json"))
                out = json.loads(buf.getvalue())
                self.assertEqual(code, 1, as_of)
                self.assertIn("run check.py --refresh-bands", " | ".join(out["results"][0]["reasons"]))
                self.assertIn("no valid as_of date", " ".join(out["warnings"]))
                # exit-only still goes through
                ex, pp = os.path.join(d, "exit.json"), os.path.join(d, "pf.json")
                with open(ex, "w") as fh:
                    json.dump({"orders": [Exits().sell()]}, fh)
                with open(pp, "w") as fh:
                    json.dump(Exits.PF, fh)
                args = self.base(d, "--bands", cache, "--now", NOW, "--offline", d)
                args[args.index("--orders") + 1] = ex
                args[args.index("--portfolio") + 1] = pp
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(check.main(args), 0, as_of)

    def test_a_raw_csv_takes_its_file_time_as_as_of(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, "sec_list.csv")
            with open(SEC_LIST, encoding="utf-8") as src, open(old, "w", encoding="utf-8") as fh:
                fh.write(src.read())
            t = datetime.datetime(2026, 10, 26, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
            os.utime(old, (t, t))
            self.assertEqual(check.load_bands(old)[1], "2026-10-26")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(self.base(d, "--bands", old, "--now", NOW, "--offline", d, "--json"))
            self.assertEqual(code, 1)
            self.assertIn("price bands are from 2026-10-26", " | ".join(json.loads(buf.getvalue())["results"][0]["reasons"]))

    def test_a_previous_close_from_yesterday_is_not_used(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")

            def go(detail, as_of="2026-10-27T03:50:00Z"):
                with open(pp, "w") as fh:
                    json.dump({"as_of": as_of, "prices": {"DEMO": 1012.0}, "detail": {"DEMO": detail}}, fh)
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = check.main(self.base(d, "--prices", pp, "--bands", SEC_LIST, "--now", NOW, "--json"))
                return code, " | ".join(json.loads(buf.getvalue())["results"][0]["reasons"])
            code, why = go({"prev_close": 1000.0, "time": "2026-10-26T09:59:00Z"})     # 15:29 IST yesterday
            self.assertEqual(code, 1)
            self.assertIn("nse-alerts/alerts.py quote DEMO", why)
            self.assertEqual(go({"prev_close": 1000.0, "time": "2026-10-27T03:50:00Z"}), (0, ""))
            self.assertEqual(go({"prev_close": 1000.0}), (0, ""))                         # falls back to as_of
            code, why = go({"prev_close": 1000.0}, as_of="2026-10-26T10:00:00Z")
            self.assertIn("nse-alerts/alerts.py quote DEMO", why)
            code, why = go({"prev_close": None, "time": "2026-10-27T03:50:00Z"})
            self.assertEqual(code, 1)
            self.assertIn("nse-alerts/alerts.py quote DEMO", why)

    def test_errors_are_one_line(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", "/nonexistent.json", "--no-write"]), 1)
        self.assertIn("no orders file", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", DEMO_PATH, "--now", "noon", "--no-write"]), 1)
        self.assertIn("--now wants", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", DEMO_PATH, "--today", "monday", "--no-write"]), 1)
        self.assertIn("--today wants", err.getvalue())

    def test_no_us_leftovers_in_the_tool(self):
        with open(os.path.join(ROOT, "nse-risk", "check.py"), encoding="utf-8") as fh:
            text = fh.read()
        for word in ("New " + "York", "NY" + "SE", "Alp" + "aca", "trader" + "/", "pdt", "eastern", "_usd", "OCC"):
            self.assertNotIn(word, text, word)


if __name__ == "__main__":
    unittest.main()
