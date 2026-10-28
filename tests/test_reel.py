"""The reel, end to end: every order-path tool run as the user runs it, in a fresh project folder.

One real ticker and day (tests/fixtures/reel.json): RELIANCE on Tue 2026-10-27, whose 15-minute
opening-range breakout fired at 12:40 PM IST. Each tool runs as a subprocess on the captured fixtures
(--offline) with the clock pinned to that session (--now), and reads what the previous tool wrote
under nse/. The numbers asserted here are the ones the README's walkthrough quotes.

The two quotes nse-alerts reads (12:40 and 13:10) are cut from the same captured 5-minute chart the
entry timer reads, so the price, day high/low and previous close are the session's own, as of then.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from _load import ROOT, FIX

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


REEL = read_json(os.path.join(FIX, "reel.json"))
SYM, DAY, AT, ALERTS_AT = REEL["symbol"], REEL["day"], REEL["at"], REEL["alerts_at"]
NOW = "%s %s" % (DAY, AT)
GROUP = "%s-%s-1" % (SYM, DAY)


def fixture(rel):
    return os.path.join(FIX, *rel.split("/"))


def quote_as_of(chart_path, hhmm, out_dir):
    """Write out_dir/yahoo-SYM.json: a Yahoo 1d quote as of hhmm IST, built from the 5-minute bars
    that had closed by then (last close, day high/low so far, first open, the chart's previous close)."""
    doc = read_json(chart_path)
    r = doc["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    day = datetime.date.fromisoformat(DAY)
    cut = datetime.datetime.combine(day, datetime.time(int(hhmm[:2]), int(hhmm[3:])), IST)
    rows = [i for i, t in enumerate(r["timestamp"])
            if t + 300 <= cut.timestamp() and q["close"][i] is not None]
    m = r["meta"]
    meta = {"currency": m["currency"], "symbol": m["symbol"], "exchangeName": m["exchangeName"],
            "instrumentType": m["instrumentType"], "gmtoffset": m["gmtoffset"], "timezone": m["timezone"],
            "regularMarketPrice": round(q["close"][rows[-1]], 2), "regularMarketTime": int(cut.timestamp()),
            "regularMarketDayHigh": round(max(q["high"][i] for i in rows), 2),
            "regularMarketDayLow": round(min(q["low"][i] for i in rows), 2),
            "chartPreviousClose": m["chartPreviousClose"], "dataGranularity": "1d", "range": "1d"}
    out = {"chart": {"result": [{"meta": meta, "timestamp": [int(cut.timestamp())],
                                 "indicators": {"quote": [{"open": [round(q["open"][rows[0]], 2)]}]}}],
                     "error": None}}
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "yahoo-%s.json" % SYM), "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    return out_dir


class Reel(unittest.TestCase):
    """Runs the chain once (setUpClass); each test checks one step's hand-off."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.d, "nse"))
        shutil.copy(os.path.join(ROOT, "nse-budget", "config.example.json"), os.path.join(cls.d, "nse", "config.json"))
        f = REEL["fixtures"]
        chart = fixture(f["entry_chart"])
        q_entry = quote_as_of(chart, AT, os.path.join(cls.d, "quotes-%s" % AT.replace(":", "")))
        q_alert = quote_as_of(chart, ALERTS_AT, os.path.join(cls.d, "quotes-%s" % ALERTS_AT.replace(":", "")))
        steps = [
            ("universe", ["nse-scanner/scan.py", "universe", "--offline", fixture(f["universe"])]),
            ("scan", ["nse-scanner/scan.py", "orb", "--offline", fixture(f["scan_charts"]), "--now", NOW,
                      "--at", AT, "--show", "50"]),
            ("entry", ["nse-entry/entry.py", "check", SYM, "--setup", "orb", "--now", NOW, "--offline", chart,
                       "--write"]),
            ("exit", ["nse-exit/exit.py", "plan", "--stop", "or-mid", "--target", "2R", "--now", NOW, "--write"]),
            ("timemachine", ["nse-timemachine/timemachine.py", "run", "--offline", fixture(f["timemachine_chart"]),
                             "--now", NOW, "--write"]),
            ("quote", ["nse-alerts/alerts.py", "quote", SYM, "--offline", q_entry, "--now", NOW, "--write"]),
            ("budget", ["nse-budget/size.py", "--now", NOW, "--write"]),
            ("risk", ["nse-risk/check.py", "--refresh-bands", "--offline", fixture(f["bands"]), "--now", NOW]),
            ("show", ["nse-broker/ticket.py", "show", "--now", NOW]),
            ("approve", ["nse-broker/ticket.py", "approve", "--index", "1", "--reply", "yes %d %s"
                         % (REEL["budget"]["qty"], SYM), "--now", NOW]),
            ("paper", ["nse-broker/paper.py", "run", "--group", GROUP, "--offline", chart, "--approved-at", NOW]),
            ("alerts_add", ["nse-alerts/alerts.py", "add", "--from-plan"]),
            ("alerts_check", ["nse-alerts/alerts.py", "check", "--offline", q_alert, "--now",
                              "%s %s" % (DAY, ALERTS_AT)]),
        ]
        cls.out = {}
        for name, argv in steps:
            p = subprocess.run([sys.executable, os.path.join(ROOT, argv[0])] + argv[1:], cwd=cls.d,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                               encoding="utf-8")
            cls.out[name] = (p.returncode, p.stdout, p.stderr)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d)

    def nse(self, *parts):
        return read_json(os.path.join(self.d, "nse", *parts))

    def ok(self, name):
        code, text, err = self.out[name]
        self.assertEqual(code, 0, "%s failed:\n%s\n%s" % (name, text, err))
        return text

    def test_every_hand_off_file_exists(self):
        for rel in ("universe.json", "watchlist.json", "entry.json", "plan.json", "timemachine.json", "prices.json",
                    "orders.json", "alerts.json", os.path.join("state", "bands.json"),
                    os.path.join("state", "risk.json"), os.path.join("state", "broker.json")):
            self.assertTrue(os.path.isfile(os.path.join(self.d, "nse", rel)), rel)
        self.assertFalse(os.path.exists(os.path.join(self.d, "trader")))

    def test_scanner_finds_the_breakout(self):
        text = self.ok("scan")
        s = REEL["scan"]
        self.assertIn("%d of %d charts closed above their opening-range high by %s IST" % (s["hits"], s["usable"], AT),
                      text)
        wl = self.nse("watchlist.json")
        self.assertIn(SYM, [x["symbol"] for x in wl["symbols"]])
        self.assertEqual(len(wl["symbols"]), s["hits"])

    def test_entry_triggers_as_captured(self):
        self.ok("entry")
        exp = read_json(fixture("nse-entry/expected.json"))
        e = self.nse("entry.json")
        self.assertEqual((e["status"], e["time"], e["price"]),
                         ("triggered", exp["checks"]["15:30"]["trigger_time"], exp["checks"]["15:30"]["trigger_price"]))
        self.assertEqual((e["opening_range"]["high"], e["opening_range"]["low"]), (exp["or_high"], exp["or_low"]))

    def test_exit_plan_is_the_time_machine_fixture_plan(self):
        self.ok("exit")
        plan, ref = self.nse("plan.json"), read_json(fixture(REEL["fixtures"]["plan"]))
        for k in ("symbol", "date", "entry", "entry_time", "entry_bar", "stop", "target", "risk_per_share",
                  "reward_risk", "flat_by", "tick", "est_charges_per_share", "net_reward_risk"):
            self.assertEqual(plan[k], ref[k], k)

    def test_time_machine_matches_expected(self):
        text = self.ok("timemachine")
        exp = read_json(fixture("nse-timemachine/expected.json"))
        mc, hist = self.nse("timemachine.json")["mc"], self.nse("timemachine.json")["history"]
        for k in ("p_target", "p_stop", "p_neither", "p_green", "median_close", "avg_R", "avg_r_net"):
            self.assertEqual(mc[k], exp[k], k)
        for k in ("triggered", "target", "stop", "close", "avg_R", "avg_r_net"):
            self.assertEqual(hist[k], exp["history"][k], k)
        self.assertIn(exp["verdict"], text)

    def test_quote_writes_todays_previous_close(self):
        text = self.ok("quote")
        q = REEL["quote"]
        self.assertIn("%s   ₹%s" % (SYM, "{:,.2f}".format(q["price"])), text)
        self.assertIn("prev close ₹%s)  %s IST" % ("{:,.2f}".format(q["prev_close"]), NOW), text)
        # alerts check rewrites prices.json at 13:10; the previous close stays the session's own
        d = self.nse("prices.json")["detail"][SYM]
        self.assertEqual(d["prev_close"], q["prev_close"])
        self.assertTrue(d["time"].startswith(DAY))

    def test_budget_writes_three_kite_legs(self):
        text = self.ok("budget")
        b = REEL["budget"]
        self.assertIn("<- binds", [l for l in text.splitlines() if l.strip().startswith(b["binds"])][0])
        g = self.nse("orders.json")["orders"]
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["group"], GROUP)
        legs = g[0]["legs"]
        self.assertEqual([(l["role"], l["transaction_type"], l["order_type"], l["quantity"]) for l in legs],
                         [("entry", "BUY", "MARKET", b["qty"]), ("stop", "SELL", "SL-M", b["qty"]),
                          ("target", "SELL", "LIMIT", b["qty"])])
        self.assertEqual((legs[1]["trigger_price"], legs[2]["price"]), (REEL["plan"]["stop"], REEL["plan"]["target"]))
        self.assertEqual((g[0]["plan"]["max_loss"], g[0]["plan"]["charges"]), (b["max_loss"], b["charges"]))

    def test_risk_passes(self):
        text = self.ok("risk")
        self.assertIn("PASS  #1 %s" % GROUP, text)
        risk = self.nse("state", "risk.json")
        self.assertEqual((risk["pass"], risk["block"]), (1, 0))
        v = list(risk["verdicts"].values())
        self.assertEqual([(x["verdict"], x["group"], x["qty"], x["max_loss"]) for x in v],
                         [("PASS", GROUP, REEL["budget"]["qty"], REEL["budget"]["max_loss"])])
        self.assertEqual(self.nse("state", "bands.json")["as_of"], DAY)

    def test_broker_shows_three_legs_and_will_not_take_a_yes_on_a_pinned_clock(self):
        # The tickets render, but a verdict from a pinned clock is never READY against the real one:
        # the yes is refused and nothing is approved. The replay below goes through paper.py --approved-at.
        code, text, err = self.out["show"]
        for leg in ("leg 1, entry: BUY %d %s" % (REEL["budget"]["qty"], SYM),
                    "leg 2, stop: SELL %d %s" % (REEL["budget"]["qty"], SYM),
                    "leg 3, target: SELL %d %s" % (REEL["budget"]["qty"], SYM)):
            self.assertIn(leg, text)
        self.assertIn("PAPER", text)
        self.assertIn("RECHECK: the risk check ran with a pinned clock (%s IST, not now)" % NOW, text)
        self.assertNotIn("READY: nse-risk PASS", text)
        code, text, err = self.out["approve"]
        self.assertEqual(code, 1)
        self.assertIn("not ready: RECHECK", err)

    def test_paper_replay_pnl(self):
        text = self.ok("paper")
        p = REEL["paper"]
        fills = [o for o in self.nse("state", "broker.json")["orders"] if o.get("status") == "filled"]
        self.assertEqual([(o["leg"], round(o["price"], 2), o["time"]) for o in fills],
                         [("entry", p["entry_fill"], p["entry_time"]), (p["exit_leg"], p["exit_fill"], p["exit_time"])])
        last = fills[-1]
        self.assertEqual((last["gross_pnl"], last["charges"], last["net_pnl"]), (p["gross_pnl"], p["charges"], p["net_pnl"]))
        self.assertIn("net -₹%.2f" % -p["net_pnl"], text)

    def test_alerts_ring_the_stop(self):
        self.ok("alerts_add")
        rules = self.nse("alerts.json")["rules"]
        self.assertEqual(sorted(r["id"] for r in rules),
                         sorted("%s-%s" % (SYM.lower(), k) for k in ("plan-stop", "plan-target", "flat-by")))
        text = self.ok("alerts_check")
        self.assertIn(REEL["alerts"]["rang"], text)

    def test_readme_walkthrough_quotes_these_numbers(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        tm = read_json(fixture("nse-timemachine/expected.json"))
        rs = lambda x: "₹" + "{:,.2f}".format(abs(x))
        b, p = REEL["budget"], REEL["paper"]
        for s in ("%s, Tuesday October 27 2026" % SYM,
                  "of the %d usable charts" % REEL["scan"]["usable"], "%d had closed above" % REEL["scan"]["hits"],
                  "closed at %s" % rs(REEL["plan"]["entry"]), rs(REEL["plan"]["stop"]), rs(REEL["plan"]["target"]),
                  "target first %.1f %%" % (100 * tm["p_target"]), "stop first %.1f %%" % (100 * tm["p_stop"]),
                  "Average R %.3f before charges, %.3f net" % (tm["avg_R"], tm["avg_r_net"]),
                  "fired %d times, %d hit the target, %d the stop, %d neither"
                  % tuple(tm["history"][k] for k in ("triggered", "target", "stop", "close")),
                  "= %d shares" % b["qty"], "%s charges, %s" % (rs(b["charges"]), rs(b["max_loss"])),
                  "BUY %d at %s" % (b["qty"], rs(p["entry_fill"])), "SELL %d at %s" % (b["qty"], rs(p["exit_fill"])),
                  "gross -%s, charges %s, **net -%s**" % (rs(p["gross_pnl"]), rs(p["charges"]), rs(p["net_pnl"])),
                  "RELIANCE %s, at or below your stop %s" % ("₹1,192.10", rs(REEL["plan"]["stop"]))):
            self.assertIn(s, readme)
        self.assertIn(REEL["alerts"]["rang"].split(" is ")[1].split(":")[0], readme)

    def test_reel_record_is_consistent(self):
        tm = read_json(fixture("nse-timemachine/expected.json"))
        self.assertEqual((REEL["symbol"], REEL["day"]), (tm["symbol"], tm["plan_date"]))
        self.assertEqual(REEL["plan"]["stop"], read_json(fixture(REEL["fixtures"]["plan"]))["stop"])


if __name__ == "__main__":
    unittest.main()
