"""nse-exit tests. The demo is synthetic: entry 1015.00, opening range 1012.40 / 998.10. Bars for the
ATR stop reuse tests/fixtures/nse-entry/DEMO-5m-synthetic.json (not market data)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, FIX, ROOT

exitp = load("nse-exit", "exit")

DEMO_BARS = os.path.join(FIX, "nse-entry", "DEMO-5m-synthetic.json")
DEMO_ENTRY = os.path.join(FIX, "nse-exit", "entry-demo.json")

with open(DEMO_ENTRY, encoding="utf-8") as _fh:
    ENTRY_DOC = json.load(_fh)

# nse/plan.json for the demo, as the contract writes it
DEMO_PLAN = {"created_by": "nse-exit", "symbol": "DEMO", "side": "buy", "date": "2026-10-27", "setup": "orb",
             "entry": 1015.0, "entry_time": "09:35 AM", "entry_bar": 3, "interval": "5m",
             "opening_range": {"high": 1012.4, "low": 998.1, "minutes": 15},
             "stop": 1005.2, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
             "target": 1034.6, "target_r": 2.0, "target_label": "2R (two times the risk)",
             "risk_per_share": 9.8, "reward_risk": 2.0, "flat_by": "15:10", "tick": "0.10",
             "exchange": "NSE", "product": "MIS"}


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=1012.40, ol=998.10, atr=None, n=None):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n)


class Math(unittest.TestCase):
    def test_the_demo_or_mid_2r(self):
        p = c(1015.00, "or-mid", "2R")
        self.assertEqual(p["stop"], Decimal("1005.20"))          # floor_tick(1005.25, 0.10)
        self.assertEqual(p["risk"], Decimal("9.80"))
        self.assertEqual(p["target"], Decimal("1034.60"))
        self.assertEqual(p["reward_risk"], Decimal("2.00"))
        self.assertEqual(p["tick"], Decimal("0.10"))
        self.assertEqual(p["target_label"], "2R (two times the risk)")
        self.assertIn("rounded down", p["stop_detail"])

    def test_charges_and_net_r(self):
        p = c(1015.00, "or-mid", "2R")
        ref = exitp.CHARGE_REF_QTY
        self.assertEqual(ref, 100)
        want = (exitp.round_trip(ref, "1015.00", "1034.60") / ref).quantize(Decimal("0.01"))
        self.assertEqual(p["est_charges_per_share"], want)
        ec = p["est_charges_per_share"]
        net = ((Decimal("1034.60") - Decimal("1015.00") - ec) / (Decimal("9.80") + ec)).quantize(Decimal("0.01"))
        self.assertEqual(p["net_reward_risk"], net)
        self.assertLess(p["net_reward_risk"], p["reward_risk"])

    def test_default_is_2r(self):
        p = c(1015.00, "or-mid")
        self.assertEqual(p["target"], Decimal("1034.60"))

    def test_raw_yahoo_floats_give_the_same_prices(self):
        p = c(1014.9999999999999, "or-mid", "2R", 1012.4000244140625, 998.0999755859375)
        self.assertEqual((p["entry"], p["stop"], p["target"]), (Decimal("1015.00"), Decimal("1005.20"), Decimal("1034.60")))

    def test_tick_slab_comes_from_the_entry_price(self):
        self.assertEqual(c(1015, "price:1005.24", "1R")["stop"], Decimal("1005.20"))
        self.assertEqual(c(200, "price:198.123", "1R")["stop"], Decimal("198.12"))      # 0.01 below 250
        self.assertEqual(c(500, "price:495.07", "1R")["stop"], Decimal("495.05"))       # 0.05 at 250-1000
        self.assertEqual(c(6000, "price:5950.7", "1R")["stop"], Decimal("5950.50"))     # 0.50 at 5000-10000

    def test_other_stops(self):
        p = c(1015.00, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("998.10"), Decimal("16.90"), Decimal("1048.80")))
        p = c(1015.00, "pct:1")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1004.80"), Decimal("10.20"), Decimal("1035.40")))
        p = c(1015.00, "atr:1.5", atr=9.625, n=4)
        self.assertEqual(p["stop"], Decimal("1000.50"))              # 1015 - 14.4375 = 1000.5625
        self.assertEqual(p["stop_label"], "1.5 x ATR(4) under the entry")
        self.assertIn("fewer than 14", p["stop_detail"])

    def test_target_rounds_to_nearest_tick_never_toward_the_entry(self):
        # entry 1015 (tick 0.10), stop 1005.00, risk 10, 1.33R = 1028.30 exactly: on a tick
        self.assertEqual(c(1015, "price:1005", "1.33R")["target"], Decimal("1028.30"))
        # risk 9.80 x 1.5 = 14.70 -> 1029.70 exact; risk 10 x 1.234 = 12.34 -> 1027.34: nearest tick 1027.30 is
        # toward the entry, so it goes up to 1027.40
        self.assertEqual(c(1015, "price:1005", "1.234R")["target"], Decimal("1027.40"))
        # 1027.36 -> nearest tick is 1027.40 (already away)
        self.assertEqual(c(1015, "price:1005", "1.236R")["target"], Decimal("1027.40"))
        # a price target is held to the same rule
        self.assertEqual(c(1015, "or-mid", "price:1030.04")["target"], Decimal("1030.10"))
        self.assertEqual(c(1015, "or-mid", "price:1030.06")["target"], Decimal("1030.10"))
        p = c(1015, "or-mid", "price:1030")
        self.assertEqual(p["target_label"], "your price (1.53R)")

    def test_refusals_say_why(self):
        cases = [("price:1015", "2R", "at or above the entry"),
                 ("price:1016", "2R", "at or above the entry"),
                 ("or-mid", "price:1014", "at or below the entry"),
                 ("price:1014.80", "2R", "less than 3 ticks"),      # 0.20 = 2 ticks
                 ("pct:0.01", "2R", "too tight to survive the spread")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(1015, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        # exactly 3 ticks (0.30) clears the tick check (the charges rule then catches it)
        with self.assertRaises(exitp.PlanError) as cm:
            c(1015, "price:1014.70")
        self.assertNotIn("ticks", str(cm.exception))
        self.assertIn("charges", str(cm.exception))
        with self.assertRaises(exitp.PlanError):
            c(1015.00, "price:1014.80")
        with self.assertRaises(exitp.PlanError):
            c(990.00, "or-mid")                                     # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(1015, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(1015, "or-mid", oh=998.10, ol=1012.40)

    def test_charges_over_half_the_risk_refuse(self):
        # 180.00 with a 40-paise stop: round trip is about 19 paise a share. Within half of 0.40 (0.20): a plan,
        # with net R well under the gross 1.0.
        p = c(180.00, "price:179.60", "1R", None, None)
        self.assertEqual(p["est_charges_per_share"], Decimal("0.19"))
        self.assertEqual(p["net_reward_risk"], Decimal("0.36"))
        # a 30-paise stop: 0.19 is over half of 0.30, so refuse
        with self.assertRaises(exitp.PlanError) as cm:
            c(180.00, "price:179.70", "1R", None, None)
        self.assertEqual(str(cm.exception),
                         "charges ≈ ₹0.19/share are over half the ₹0.30 risk/share: widen the stop or skip")
        # a bigger target does not rescue a tight stop on a cheap stock
        with self.assertRaises(exitp.PlanError) as cm:
            c(180.00, "price:179.70", "2R", None, None)
        self.assertIn("widen the stop or skip", str(cm.exception))

    def test_config_charges_override(self):
        sr, sp = exitp.parse_stop("price:179.70")
        tr, tp = exitp.parse_target("1R")
        p = exitp.compute(180.00, sr, sp, tr, tp, cfg_charges={"brokerage_flat": 0, "brokerage_pct": 0})
        self.assertLess(p["est_charges_per_share"], Decimal("0.15"))

    def test_atr_on_the_session_bars(self):
        ch = exitp.load_chart("DEMO", "5m", None, DEMO_BARS)
        day = exitp.datetime.date(2026, 10, 27)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=3)
        self.assertEqual(len(bars), 4)                              # 09:15 ... the 09:30 entry bar
        trs, prev = [], None
        for b in bars:
            r = b["h"] - b["l"]
            if prev is not None:
                r = max(r, abs(b["h"] - prev), abs(b["l"] - prev))
            trs.append(r)
            prev = b["c"]
        self.assertAlmostEqual(trs[0], 1008.0 - 998.1, places=4)    # no overnight gap on the first bar
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 4)
        self.assertAlmostEqual(atr, sum(trs) / 4, places=9)
        self.assertAlmostEqual(atr, 9.625, places=4)
        all_bars = exitp.session_bars(ch, day, "5m")
        self.assertGreater(len(all_bars), 14)
        self.assertEqual(exitp.atr_of(all_bars)[1], 14)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("nse")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(exitp, ["plan", "--now", "2026-10-27 09:36"] + list(args))

    def read_plan(self):
        with open(os.path.join("nse", "plan.json")) as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        shutil.copy(DEMO_ENTRY, os.path.join("nse", "entry.json"))
        code, text, err = self.plan("--stop", "or-mid", "--target", "2R", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in DEMO_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["exchange"], "NSE")
        self.assertEqual(p["product"], "MIS")
        self.assertIsInstance(p["tick"], str)
        self.assertIsInstance(p["est_charges_per_share"], float)
        self.assertIsInstance(p["net_reward_risk"], float)
        self.assertEqual(p["est_charges_per_share"], round(float(
            exitp.round_trip(100, "1015.00", "1034.60") / 100), 2))
        self.assertLess(p["net_reward_risk"], 2.0)
        self.assertIn("Stop          ₹1,005.20  opening-range midpoint", text)
        self.assertIn("Target        ₹1,034.60  2R (two times the risk)", text)
        self.assertIn("Risk a share  ₹9.80", text)
        self.assertIn("Reward:risk   2.0", text)
        self.assertIn("Net R", text)
        self.assertIn("Flat by       15:10 IST", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("/nse-timemachine", text)
        self.assertNotIn("pro" + "fit", text.lower())   # never on screen
        code, text, _ = self.plan("--write")                       # the defaults: or-mid, 2R
        self.assertEqual(self.read_plan()["target"], 1034.60)
        code, text, _ = run(exitp, ["show", "--now", "2026-10-27 09:40"])
        self.assertEqual(code, 0)
        self.assertIn("₹1,034.60", text)
        self.assertNotIn("not today", text)
        code, text, _ = run(exitp, ["show", "--now", "2026-10-28 09:40"])
        self.assertIn("not today", text)

    def test_manual_flags_and_json(self):
        code, text, _ = self.plan("--symbol", "demo", "--entry", "1015", "--or-high", "1012.40", "--or-low", "998.10",
                                  "--time", "09:35 AM", "--entry-bar", "3", "--date", "2026-10-27", "--json")
        self.assertEqual(code, 0)
        p = json.loads(text)
        for k, v in DEMO_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["warnings"], [])

    def test_atr_stop_reads_bars(self):
        shutil.copy(DEMO_ENTRY, os.path.join("nse", "entry.json"))
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", DEMO_BARS, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (1000.5, 14.5, 1044.0))
        self.assertEqual((p["stop_rule"], p["atr_bars"]), ("atr", 4))
        self.assertAlmostEqual(p["atr"], 9.625, places=4)
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", DEMO_BARS, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("nse", "entry.json"), dict(ENTRY_DOC, status="waiting", time=None, price=None,
                                                           bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("DEMO is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "1012.5", "--json")    # plan ahead at the level
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (1012.5, 1005.2, None))

        self.write(os.path.join("nse", "entry.json"), dict(ENTRY_DOC, setup="level", level=1012.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("longs only", err)
        self.write(os.path.join("nse", "entry.json"), dict(ENTRY_DOC, side="sell"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        os.remove(os.path.join("nse", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        code, _, err = self.plan("--symbol", "DEMO")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "DEMO", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "DEMO", "--entry", "1015", "--or-high", "1012.4")
        self.assertIn("both --or-high and --or-low", err)
        self.assertNotIn("Traceback", err)

    def test_charges_refusal_writes_no_plan(self):
        code, _, err = self.plan("--symbol", "CHEAP", "--entry", "180", "--setup", "level", "--stop", "price:179.70",
                                 "--target", "1R", "--write")
        self.assertEqual(code, 1)
        self.assertIn("widen the stop or skip", err)
        self.assertFalse(os.path.exists(os.path.join("nse", "plan.json")))

    def test_flat_by_config_and_square_off_warning(self):
        shutil.copy(DEMO_ENTRY, os.path.join("nse", "entry.json"))
        code, text, _ = self.plan("--json")
        self.assertEqual(json.loads(text)["flat_by"], "15:10")      # the default
        self.write(os.path.join("nse", "config.json"), {"flat_by": "14:50", "min_reward_risk": 2.5})
        code, text, _ = self.plan("--json")
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "14:50")
        self.assertTrue(any("under your min_reward_risk 2.5" in w for w in p["warnings"]), p["warnings"])
        os.remove(os.path.join("nse", "config.json"))

        code, text, _ = self.plan("--flat-by", "15:25", "--json")
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "15:25")
        self.assertTrue(any("after 15:20 your broker may auto square-off MIS" in w for w in p["warnings"]))
        code, text, _ = self.plan("--flat-by", "15:20", "--json")
        self.assertEqual(json.loads(text)["warnings"], [])
        code, text, _ = self.plan("--flat-by", "15:25")
        self.assertIn("after 15:20 your broker may auto square-off MIS", text)
        code, _, err = self.plan("--flat-by", "15:30")
        self.assertEqual(code, 1)
        self.assertIn("at or after the 15:30 close", err)
        code, _, err = self.plan("--flat-by", "9:35")
        self.assertEqual(code, 1)
        self.write(os.path.join("nse", "entry.json"), dict(ENTRY_DOC, time="03:12 PM"))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)

    def test_holiday_and_calendar_warnings(self):
        shutil.copy(DEMO_ENTRY, os.path.join("nse", "entry.json"))
        self.write(os.path.join("nse", "entry.json"), dict(ENTRY_DOC, date="2026-11-10"))
        code, text, _ = run(exitp, ["plan", "--now", "2026-11-10 09:36", "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(any("Market closed" in w and "Diwali-Balipratipada" in w for w in json.loads(text)["warnings"]))
        self.write(os.path.join("nse", "entry.json"), dict(ENTRY_DOC, date="2027-02-02"))
        code, text, _ = run(exitp, ["plan", "--now", "2027-02-02 09:36", "--json"])
        self.assertTrue(any("NSE calendar for 2027 not loaded" in w for w in json.loads(text)["warnings"]))

    def test_show_without_a_plan(self):
        code, text, _ = run(exitp, ["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "nse-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: nse-exit\n"))
        self.assertGreater(len(text.splitlines()), 60)
        fm = text.split("---\n")[1]
        self.assertGreater(len(fm), 200)
        low = " ".join(text.lower().split())
        for must in ("before the entry", "a stop is a plan, not a guarantee", "day trading loses most people money",
                     "/nse-timemachine", "/nse-budget", "longs only", "₹1,005.20", "₹1,034.60", "₹9.80",
                     "net r", "charges", "not financial advice", "yes", "ist", "nse/plan.json", "15:10",
                     "too tight to survive the spread", "tick"):
            self.assertIn(must, low)
        for never in (chr(0x2014), "tick " + "by tick", "real " + "time", "pro" + "fit", "trader/", "new york", "nyse"):
            self.assertNotIn(never, low)
        self.assertNotRegex(text, r"\$\d")


class NoUsLeftovers(unittest.TestCase):
    def test_exit_py_is_nse(self):
        with open(os.path.join(ROOT, "nse-exit", "exit.py"), encoding="utf-8") as fh:
            src = fh.read()
        for word in ("NYSE", "New York", "eastern", "trader/", "trader-exit", "floor_cents", "Jakeschincariol"):
            self.assertNotIn(word, src, word)


if __name__ == "__main__":
    unittest.main()
