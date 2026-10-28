"""nse-timemachine against RELIANCE.NS's real 60 days of 5-minute bars (captured with tests/capture.py
nse-timemachine) and the plan nse-entry + nse-exit make from the 2026-10-27 session in that data.
expected.json holds what the tool said about them, seed 19, when they were captured."""

import copy
import gzip
import hashlib
import io
import json
import os
import random
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tm = load("nse-timemachine", "timemachine")

DIR = os.path.join(FIX, "nse-timemachine")
CHART = os.path.join(DIR, "RELIANCE-60d5m.json.gz")   # Yahoo v8 chart, range=60d interval=5m, RELIANCE.NS
with gzip.open(CHART, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
with open(os.path.join(DIR, "plan-RELIANCE.json"), encoding="utf-8") as _fh:
    PLAN = json.load(_fh)            # as nse-exit writes it: 2026-10-27, entry 1197.20 at 12:40 PM, 2R
with open(os.path.join(DIR, "expected.json"), encoding="utf-8") as _fh:
    EXP = json.load(_fh)
NOW = EXP["now"]                     # the evening of the plan's day
NOW_UTC = tm.parse_now(NOW)


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=TEXT, now=NOW_UTC, **kw):
    return tm.time_machine(text, p or plan(), now, **kw)


def edited(fn, text=TEXT):
    """The fixture's chart JSON, changed by fn(result), back as text."""
    doc = json.loads(text)
    fn(doc["chart"]["result"][0])
    return json.dumps(doc)


def keep_bars(result, keep):
    """Keep only the bars where keep(ts) is true."""
    q = result["indicators"]["quote"][0]
    idx = [i for i, ts in enumerate(result["timestamp"]) if keep(ts)]
    result["timestamp"] = [result["timestamp"][i] for i in idx]
    for k in q:
        q[k] = [q[k][i] for i in idx]


def ist(ts):
    return tm.to_ist(tm.utc_of(ts))


def minute_of(ts):
    t = ist(ts)
    return t.hour * 60 + t.minute


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


class Expected(unittest.TestCase):
    """The seeded run on the fixture reproduces expected.json exactly."""

    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_seed_19_reproduces_expected_json(self):
        m, h = self.doc["mc"], self.doc["history"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 19, 2, "15:10"))
        for key in ("pool_sessions", "pool_from", "pool_to", "bars_left", "p_target", "p_stop", "p_neither",
                    "p_green", "median_close", "p5_close", "p95_close", "avg_R", "avg_r_net"):
            self.assertEqual(m[key], EXP[key], key)
        for key in ("sessions", "triggered", "target", "stop", "close", "avg_R", "avg_r_net"):
            self.assertEqual(h[key], EXP["history"][key], key)
        self.assertEqual(self.doc["data"]["short_sessions_left_out"], EXP["excluded_sessions"])
        self.assertEqual(self.doc["verdict"], EXP["verdict"])

    def test_the_odds_are_pinned(self):
        # a second copy of the numbers: a regenerated expected.json cannot silently move them
        m = self.doc["mc"]
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"], m["p_green"]), (0.1553, 0.5866, 0.2581, 0.3399))
        self.assertEqual((m["median_close"], m["p5_close"], m["p95_close"]), (1196.38, 1185.9, 1206.96))
        self.assertEqual((m["avg_R"], m["avg_r_net"], m["r_at_target"]), (-0.156, -0.314, 2.0))
        self.assertEqual((m["pool_sessions"], m["pool_from"], m["pool_to"]), (57, "2026-08-03", "2026-10-26"))

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"], h["target"], h["stop"], h["close"]), (57, 25, 7, 11, 7))
        self.assertEqual((h["avg_R"], h["avg_r_net"]), (0.167, -0.013))
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 2.0})
        for t in h["trades"]:       # stops round down, targets to the nearest tick, never toward the entry
            tick = float(tm.tick_for(t["entry"]))
            self.assertTrue(t["stop"] < t["entry"] < t["target"], t)
            for px in ("entry", "stop", "target"):
                self.assertAlmostEqual(t[px] / tick, round(t[px] / tick), places=6, msg=t)
            self.assertLessEqual(t["R_net"], t["R"], t)

    def test_the_verdict(self):
        self.assertEqual(self.doc["verdict"].split(":")[0], "Leans toward the stop")
        gap, share, even = tm.lean(self.doc["mc"])
        self.assertEqual((round(share, 3), round(even, 3)), (0.209, 0.333))
        self.assertEqual(self.doc["verdict_notes"], [])

    def test_bands_start_at_the_entry(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [1197.2] * 5)
        self.assertEqual(len(m["bands"]), 31)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("12:40", "15:10", 31))
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertTrue(all(p[0] == 1197.2 and len(p) == 31 for p in m["sample_paths"]))

    def test_the_plan_is_carried_through(self):
        p = self.doc["plan"]
        self.assertEqual((p["symbol"], p["entry"], p["stop"], p["target"], p["entry_bar"], p["entry_time"]),
                         ("RELIANCE", 1197.2, 1193.3, 1205.0, 40, "12:40 PM"))
        self.assertEqual((p["risk_per_share"], p["target_r"], p["flat_by"], p["est_charges_per_share"]),
                         (3.9, 2.0, "15:10", 0.9))
        self.assertEqual(self.doc["data"]["symbol"], "RELIANCE.NS")
        self.assertEqual(self.doc["warnings"], [])

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("RELIANCE buy, Tuesday 2026-10-27: entry ₹1,197.20 at 12:40 PM IST (bar 40), "
                     "stop ₹1,193.30, target ₹1,205.00 (2R, risk ₹3.90 a share)",
                     "10,000 replays", "30 five-minute bars", "03:10 PM IST", "seed 19", "57 full sessions",
                     "target first   15.5 %", "stop first     58.7 %",
                     "neither        25.8 %   still open at 03:10 PM IST", "green          34.0 %",
                     "median ₹1,196.38, 90 % of replays between ₹1,185.90 and ₹1,206.96",
                     "Average R: -0.156 before charges, -0.314 net of charges",
                     "fired 25 times: 7 target, 11 stop, 7 neither. Average R +0.167 before charges, -0.013 net",
                     "Leans toward the stop: at 2R", "Left out 1 short session", "2026-10-15",
                     "not a prediction", "--seed 19"):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "real time", "tick by tick", "$"):
            self.assertNotIn(word, text)


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        a = machine(replays=2000, seed=5)["mc"]
        b = machine(replays=2000, seed=5)["mc"]
        self.assertEqual(a, b)

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertNotEqual((m["p_target"], m["p_stop"], m["median_close"]), (0.1553, 0.5866, 1196.38))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)

    def test_the_draws_are_stable_across_pythons(self):
        # random.Random(seed), randint and choice are stable since Python 3.2; the fixture numbers above
        # are the real proof, this pins the first draws so a failure points at the RNG
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(59)), 18)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)

    def test_simulate_signature_and_walk(self):
        # a pool where every bar closes 0.1 % up and never touches anything: 30 bars are walked, no more
        flat = [[(1.0, 1.0005, 0.9998, 1.001)] if i else [] for i in range(tm.SESSION_BARS)]
        mc = tm.simulate(flat, 1000.0, 990.0, 1100.0, 40, 70, 50, 19, 2, "buy")
        self.assertEqual(mc["bars_left"], 30)
        self.assertEqual(mc["p_neither"], 1.0)
        self.assertEqual(mc["median_close"], round(1000.0 * 1.001 ** 30, 2))
        self.assertEqual(len(mc["sample_paths"][0]), 31)
        self.assertEqual(len(mc["_exits"]), 50)


class Session(unittest.TestCase):
    """75 five-minute slots, 09:15 to 15:30 IST; the replay ends at flat_by."""

    def test_the_slots(self):
        self.assertEqual((tm.SESSION_BARS, tm.OPEN_MIN, tm.CLOSE_MIN), (75, 9 * 60 + 15, 15 * 60 + 30))
        self.assertEqual(tm.end_slot(tm.parse_clock("15:10", "x")), 71)
        self.assertEqual(tm.end_slot(tm.parse_clock("15:30", "x")), 75)
        self.assertEqual(tm.end_slot(tm.parse_clock("09:20", "x")), 1)

    def test_flat_by_1510_stops_the_replay_at_slot_71(self):
        doc = machine()
        m = doc["mc"]
        self.assertEqual(m["end_slot"], 71)                       # the 15:10 bar is not replayed
        self.assertEqual(m["bars_left"], 70 - 40)                 # bars 41..70, the last one starts 15:05
        self.assertEqual(m["band_times"][-1], "15:10")            # marked at the close of the 15:05 bar
        self.assertEqual(doc["plan"]["flat_by"], "15:10")

    def test_an_earlier_flat_by(self):
        doc = machine(plan(flat_by="14:30"), replays=300)
        self.assertEqual((doc["mc"]["end_slot"], doc["mc"]["until"], doc["mc"]["bars_left"]), (63, "14:30", 22))
        self.assertIn("still open at 02:30 PM IST", "\n".join(tm.report(doc)))
        # --until beats the plan, the config fills in when the plan has no flat_by
        self.assertEqual(machine(plan(flat_by="14:30"), until="15:00", replays=50)["mc"]["until"], "15:00")
        self.assertEqual(machine(plan(flat_by=None), replays=50, cfg={"flat_by": "14:00"})["mc"]["until"], "14:00")
        self.assertEqual(machine(plan(flat_by=None), replays=50)["mc"]["until"], "15:10")

    def test_until_checks(self):
        for bad, words in (("12:40 PM", "nothing to replay"), ("15:35", "no later than 03:30 PM"),
                           ("15:12", "5-minute grid"), ("noon", "IST time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)
        self.assertEqual(machine(until="3:00 PM", replays=50)["mc"]["until"], "15:00")

    def test_yahoos_missing_closing_bars_are_not_a_gap(self):
        # the real feed leaves out 15:20 and 15:25 (and 15:15 now and then); those sessions still count
        _, bars, dropped = tm.parse_bars(tm.chart_result(TEXT, "RELIANCE"), NOW_UTC)
        days = tm.split_sessions(bars)
        self.assertIn(len(days["2026-08-05"]), (72, 73, 74))
        self.assertNotIn(73, days["2026-08-05"])
        self.assertGreater(dropped["no_price"], 100)
        full, short = tm.full_sessions(days, "2026-10-27")
        self.assertEqual((len(full), short), (57, [{"date": "2026-10-15", "bars": 72}]))

    def test_a_replay_past_what_yahoo_has_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(until="15:30", replays=10)
        self.assertIn("closing minutes", str(cm.exception))
        self.assertIn("--until", str(cm.exception))


class Bars(unittest.TestCase):
    def setUp(self):
        self.result = tm.chart_result(TEXT, "RELIANCE")

    def test_sessions_are_split_by_ist_date(self):
        _, bars, _ = tm.parse_bars(self.result, NOW_UTC)
        first = [b for b in bars if b["date"] == "2026-10-27"][0]
        self.assertEqual((first["start"], first["slot"]), ("09:15", 0))
        slot71 = [b for b in bars if b["date"] == "2026-10-27" and b["slot"] == 71][0]
        self.assertEqual(slot71["start"], "15:10")
        entry = [b for b in bars if b["date"] == "2026-10-27" and b["slot"] == 40][0]
        self.assertEqual(entry["start"], "12:35")            # the bar that closes at 12:40

    def test_ist_is_fixed_and_the_clock_edges(self):
        stamps = [1793072700,   # 2026-10-27 03:45Z = 09:15 IST, slot 0
                  1793086200,   # 2026-10-27 07:30Z = 13:00 IST, slot 45
                  1793094600,   # 2026-10-27 09:50Z = 15:20 IST, slot 73
                  1793072400,   # 2026-10-27 03:40Z = 09:10 IST, pre-open: dropped
                  1793095200,   # 2026-10-27 10:00Z = 15:30 IST, the close: dropped
                  1793037300,   # 2026-10-26 18:05Z = 2026-10-26 23:35 IST: dropped, not the 26th's session
                  1793052000]   # 2026-10-26 22:00Z = 2026-10-27 03:30 IST: dropped, not the 26th's session
        n = len(stamps)
        res = {"meta": {"symbol": "X.NS"}, "timestamp": stamps,
               "indicators": {"quote": [{"open": [1] * n, "high": [1] * n, "low": [1] * n, "close": [1] * n,
                                         "volume": [0] * n}]}}
        _, bars, dropped = tm.parse_bars(res, tm.parse_now("2026-10-28T00:00Z"))
        self.assertEqual([(b["date"], b["start"], b["slot"]) for b in bars],
                         [("2026-10-27", "09:15", 0), ("2026-10-27", "13:00", 45), ("2026-10-27", "15:20", 73)])
        self.assertEqual(dropped["outside_hours"], 4)

    def test_the_in_progress_tick_is_dropped(self):
        def add_tick(res):     # a live tick off the 5-minute grid, 12:49:45 IST
            ts = res["timestamp"]
            res["timestamp"] = ts + [1793081985]
            for k, v in res["indicators"]["quote"][0].items():
                v.append(1197.0)
        res = tm.chart_result(edited(add_tick), "RELIANCE")
        _, bars, dropped = tm.parse_bars(res, NOW_UTC)
        self.assertEqual(dropped["off_grid"], 1)

    def test_now_pins_the_clock(self):
        bars = tm.parse_bars(self.result, tm.parse_now("2026-10-27 12:30"))[1]
        today = [b for b in bars if b["date"] == "2026-10-27"]
        self.assertEqual(today[-1]["start"], "12:25")        # 12:30 is still forming at 12:30
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-10-27 12:30"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("12:25 bar", str(cm.exception))
        self.assertEqual(machine(now=tm.parse_now("2026-10-27 12:40"), replays=200)["mc"]["pool_sessions"], 57)

    def test_a_short_session_is_left_out_of_pool_and_history(self):
        def cut(res):     # 2026-09-04 with only its first 40 bars (a special session or a feed outage)
            keep_bars(res, lambda ts: not (ist(ts).date().isoformat() == "2026-09-04"
                                           and minute_of(ts) >= tm.OPEN_MIN + 5 * 40))
        doc = machine(text=edited(cut), replays=300)
        self.assertEqual(doc["mc"]["pool_sessions"], 56)
        self.assertEqual(doc["mc"]["sessions_excluded"], 2)
        self.assertEqual(doc["data"]["short_sessions_left_out"],
                         [{"date": "2026-09-04", "bars": 40}, {"date": "2026-10-15", "bars": 72}])
        self.assertEqual(doc["history"]["sessions"], 56)
        self.assertNotIn("2026-09-04", [t["date"] for t in doc["history"]["trades"]])
        self.assertIn("Left out 2 short session(s)", "\n".join(tm.report(doc)))

    def test_a_muhurat_session_is_left_out(self):
        def twelve(res, when):      # twelve 5-minute bars starting at `when` (UTC)
            base = int((when - tm.EPOCH).total_seconds())
            res["timestamp"] += [base + 300 * i for i in range(12)]
            for k, v in res["indicators"]["quote"][0].items():
                v += [1200.0] * 12
        utc = tm.datetime.timezone.utc

        # an evening session (18:00 IST Sunday 2026-11-08) is outside 09:15-15:30: dropped bar by bar
        evening = edited(lambda res: twelve(res, tm.datetime.datetime(2026, 11, 8, 12, 30, tzinfo=utc)))
        doc = machine(text=evening, replays=200, now=tm.parse_now("2026-11-09 00:00"))
        self.assertEqual(doc["data"]["dropped"]["outside_hours"], 12)
        self.assertEqual(doc["mc"]["pool_sessions"], 57)

        # a 12-bar session inside the hours (13:45 IST) is a short session: out of the pool and the history
        midday = edited(lambda res: twelve(res, tm.datetime.datetime(2026, 10, 24, 8, 15, tzinfo=utc)))
        doc = machine(text=midday, replays=200)
        self.assertEqual(doc["mc"]["pool_sessions"], 57)
        self.assertEqual(doc["history"]["sessions"], 57)
        self.assertIn({"date": "2026-10-24", "bars": 12}, doc["data"]["short_sessions_left_out"])
        self.assertEqual(doc["mc"]["sessions_excluded"], 2)

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200)
        self.assertEqual(doc["mc"]["pool_to"], "2026-10-26")
        self.assertNotIn("2026-10-27", [t["date"] for t in doc["history"]["trades"]])

    def test_a_missing_price_is_dropped(self):
        def hole(res):
            res["indicators"]["quote"][0]["close"][5] = None
        doc = machine(text=edited(hole), replays=200)
        self.assertEqual(doc["data"]["dropped"]["no_price"], 117)
        self.assertEqual(doc["mc"]["pool_sessions"], 56)     # 2026-08-03 now has a gap

    def test_empty_and_broken_data(self):
        empty = json.dumps({"chart": {"result": [{"meta": {"symbol": "RELIANCE.NS"}, "timestamp": [],
                                                   "indicators": {"quote": [{}]}}], "error": None}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=empty)
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        gone = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                               "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=gone)
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(tm.TMError):
            machine(text="<html>rate limited</html>")

    def test_fewer_than_20_sessions_is_refused(self):
        dates = [d for d, _ in tm.full_sessions(tm.split_sessions(tm.parse_bars(
            tm.chart_result(TEXT, "RELIANCE"), NOW_UTC)[1]), "2026-10-27")[0]]

        def keep_from(day):
            return lambda res: keep_bars(res, lambda ts: ist(ts).date().isoformat() >= day)
        with self.assertRaises(tm.TMError) as cm:
            machine(text=edited(keep_from(dates[-19])))
        self.assertIn("only 19 full session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))
        self.assertEqual(machine(text=edited(keep_from(dates[-20])), replays=200)["mc"]["pool_sessions"], 20)

    def test_the_data_must_be_the_plans_symbol(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="TCS"))
        self.assertIn("data is for RELIANCE.NS", str(cm.exception))


class Calendar(unittest.TestCase):
    def test_a_holiday_is_refused_by_name(self):
        with self.assertRaises(tm.TMError) as cm:       # Dussehra, a Tuesday
            machine(plan(date="2026-10-20"))
        self.assertIn("Dussehra", str(cm.exception))
        self.assertIn("not a trading day", str(cm.exception))

    def test_a_weekend_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-10-25"))
        self.assertIn("weekend", str(cm.exception))

    def test_a_special_session_is_refused(self):
        cfg = {"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-11-08"), cfg=cfg)
        self.assertIn("special session (18:00 to 19:00 IST)", str(cm.exception))

    def test_an_unloaded_year_warns_and_treats_weekdays_as_open(self):
        d = tm.datetime.date(2027, 3, 1)
        self.assertEqual(tm.calendar_warning(d),
                         "NSE calendar for 2027 not loaded: run nse-alerts/alerts.py calendar --refresh or check "
                         "nseindia.com (treating Mon-Fri as open)")
        self.assertEqual(tm.calendar_warning(tm.datetime.date(2026, 10, 27)), "")
        # data for a 2027 day: the run goes ahead and carries the warning
        shift = 365 * 86400

        def to_2027(res):
            res["timestamp"] = [t + shift for t in res["timestamp"]]
            res["meta"]["regularMarketTime"] += shift
        text = edited(to_2027)
        p = plan(date="2027-10-27")
        self.assertEqual(tm.datetime.date(2027, 10, 27).weekday(), 2)
        doc = machine(p, text=text, now=tm.parse_now("2027-10-27 23:00"), replays=200)
        self.assertEqual(doc["warnings"][0][:35], "NSE calendar for 2027 not loaded: r")
        self.assertIn("Note: NSE calendar for 2027 not loaded", "\n".join(tm.report(doc)))


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-10-30"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("no bars for that day", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="12:42 PM", entry_bar=None), "5-minute grid"),
            (dict(entry_time="3:30 PM", entry_bar=None), "between 09:20 AM and 03:25 PM"),
            (dict(entry_bar=41), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "7 days of 1-min bars"),
            (dict(stop=1198.0), "stop < entry < target"),
            (dict(side="short"), "buy or sell"),
            (dict(symbol=""), "NSE symbol"),
            (dict(symbol="TS LA"), "NSE symbol"),
            (dict(entry="abc"), "number for entry"),
            (dict(date="Oct 27"), "date like"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
            (dict(est_charges_per_share="x"), "est_charges_per_share"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)

    def test_symbols_with_ampersand_and_hyphen(self):
        for sym in ("M&M", "BAJAJ-AUTO", "J&KBANK", "m&m.ns"):
            self.assertTrue(tm.SYMBOL_RE.match(sym.upper()), sym)
        self.assertEqual(plan(symbol="M&M")["symbol"], "M&M")
        self.assertEqual(plan(symbol="bajaj-auto.NS")["symbol"], "BAJAJ-AUTO")

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "12:40 PM")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 40)
        self.assertEqual(plan(entry_time="12:40", entry_bar=None)["entry_bar"], 40)
        self.assertEqual(plan(entry_time="12:40pm", entry_bar=None)["entry_bar"], 40)
        self.assertEqual(plan(entry_time="09:35 AM", entry_bar=None)["entry_bar"], 3)
        self.assertEqual(plan(entry_time="3:10", entry_bar=None)["entry_bar"], 70)

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=1230.0, stop=1220.0, target=1250.0), replays=200)
        self.assertIn("outside that bar's range", doc["warnings"][0])

    def test_stops_round_away_from_the_entry_on_the_tick(self):
        self.assertEqual(tm.tick_away(1193.35, 1197.2, True), 1193.3)       # tick 0.10
        self.assertEqual(tm.tick_away(1193.31, 1197.2, False), 1193.4)
        self.assertEqual(tm.tick_away(212.347, 212.5, True), 212.34)        # tick 0.01
        self.assertEqual(tm.tick_away(498.52, 502.0, True), 498.5)          # tick 0.05
        self.assertEqual(tm.tick_away(1189.699951171875, 1197.2, True), 1189.7)   # Yahoo float noise
        self.assertEqual(tm.tick_away(1189.7000000000003, 1197.2, False), 1189.7)

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        rule = machine(plan(stop_rule=None), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_mid", 2.0))
        rule = machine(plan(stop_rule=None, stop=1189.7, target=1204.7, est_charges_per_share=0.9), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 1.0))
        rule = machine(plan(stop_rule="atr", stop=1190.0, target=1211.6), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("pct", 2.0))
        self.assertAlmostEqual(rule["stop_pct"], round(100.0 * 7.2 / 1197.2, 4), places=4)
        rule = machine(plan(stop_rule="price", stop=1190.0, target=1211.6), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "pct")

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="level", opening_range=None), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertEqual(doc["mc"]["pool_sessions"], 57)
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", stop=1203.1, target=1185.4, stop_rule="or_high", est_charges_per_share=0.9),
                      replays=2000)
        m = doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertEqual(m["r_at_target"], 2.0)
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_high")
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)
        self.assertIn("below the entry", "\n".join(tm.report(doc)))

    def test_the_verdict_thresholds_at_1r(self):
        # 1R: the target's share of the replays that hit one side against 50 %
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.26}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
        self.assertEqual(tm.verdict({"p_target": 0.10, "p_stop": 0.60, "r_at_target": 1.0}),
                         "Strongly toward the stop: target first 10.0 % vs stop first 60.0 %.")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "14:30"}),
                         "No replay reached the target or the stop by 02:30 PM IST: the price at 02:30 PM IST "
                         "decides every one.")

    def test_a_2r_target_is_judged_against_its_break_even(self):
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        self.assertEqual(tm.verdict_notes(mc), [])
        lean = dict(mc, p_target=0.15, p_stop=0.55)
        self.assertTrue(tm.verdict(lean).startswith("Leans toward the stop: at 2R the target has to come first in "
                                                    "33 % of the replays that hit one side; it did in 21 %"))
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])
        self.assertTrue(tm.verdict(dict(mc, p_target=0.30, p_stop=0.40)).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict(dict(mc, p_target=0.40, p_stop=0.40)).startswith("Strongly toward the target"))
        self.assertIn("at 1.5R the target has to come first in 40 %", tm.verdict(dict(mc, r_at_target=1.5)))

    def test_many_neither_and_the_charges_note(self):
        mc = {"p_target": 0.10, "p_stop": 0.10, "p_neither": 0.80, "r_at_target": 1.0, "until": "15:10",
              "avg_R": 0.06, "avg_r_net": -0.04}
        self.assertEqual(tm.verdict_notes(mc), [
            "80.0 % of the replays hit neither by 03:10 PM IST; the price at 03:10 PM IST decides those.",
            "After charges the average R is -0.040 (+0.060 before): costs eat the edge."])


class NetR(unittest.TestCase):
    def test_the_formula(self):
        # (exit - entry - c) / (entry - stop + c)
        self.assertAlmostEqual(tm.net_r(1197.2, 1193.3, 1205.0, 0.9), (7.8 - 0.9) / (3.9 + 0.9), places=9)
        self.assertAlmostEqual(tm.net_r(1197.2, 1193.3, 1193.3, 0.9), -1.0, places=9)     # the stop is -1R net too
        self.assertAlmostEqual(tm.net_r(1197.2, 1193.3, 1197.2, 0.9), -0.9 / 4.8, places=9)
        self.assertAlmostEqual(tm.net_r(1197.2, 1203.1, 1189.4, 0.9, long=False), (7.8 - 0.9) / (5.9 + 0.9),
                               places=9)

    def test_the_charge_comes_from_the_plan_or_the_charges_block(self):
        self.assertEqual(tm.charge_per_share({"est_charges_per_share": 0.9})(1197.2, 1205.0), 0.9)
        calc = tm.charge_per_share({})
        want = float(tm.round_trip(100, 1197.2, 1205.0) / 100)
        self.assertEqual(calc(1197.2, 1205.0), want)
        self.assertEqual(tm.CHARGE_REF_QTY, 100)
        self.assertGreater(want, 0)
        zero = {k: 0 for k in tm.CHARGES_DEFAULT}
        self.assertEqual(tm.charge_per_share({}, zero)(1197.2, 1205.0), 0.0)

    def test_net_is_below_gross_when_charges_are_positive(self):
        doc = machine(replays=2000)
        self.assertLess(doc["mc"]["avg_r_net"], doc["mc"]["avg_R"])
        self.assertLess(doc["history"]["avg_r_net"], doc["history"]["avg_R"])
        for t in doc["history"]["trades"]:
            if t["outcome"] == "stop":
                self.assertEqual((t["R"], t["R_net"]), (-1.0, -1.0))
            else:
                self.assertLess(t["R_net"], t["R"], t)
        # no estimate in the plan: the charges block prices every exit
        nop = machine(plan(est_charges_per_share=None), replays=2000)
        self.assertLess(nop["mc"]["avg_r_net"], nop["mc"]["avg_R"])

    def test_zero_charges_make_net_equal_gross(self):
        zero = {k: 0 for k in tm.CHARGES_DEFAULT}
        doc = machine(plan(est_charges_per_share=None), replays=2000, cfg={"charges": zero})
        self.assertEqual(doc["mc"]["avg_r_net"], doc["mc"]["avg_R"])
        self.assertEqual(doc["history"]["avg_r_net"], doc["history"]["avg_R"])

    def test_bigger_charges_cost_more(self):
        a = machine(plan(est_charges_per_share=0.5), replays=1000)["mc"]["avg_r_net"]
        b = machine(plan(est_charges_per_share=1.5), replays=1000)["mc"]["avg_r_net"]
        self.assertLess(b, a)

    def test_a_target_hit_nets_less_than_two_r(self):
        h = machine(replays=100)["history"]
        wins = [t for t in h["trades"] if t["outcome"] == "target"]
        self.assertTrue(wins)
        for t in wins:
            self.assertEqual(t["R"], 2.0)
            self.assertLess(t["R_net"], 2.0)
        for t in (x for x in h["trades"] if x["outcome"] == "stop"):
            self.assertEqual((t["R"], t["R_net"]), (-1.0, -1.0))


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = tm._get, tm._sleep
        tm._sleep = self.sleeps.append

    def tearDown(self):
        tm._get, tm._sleep = self._get, self._sleep

    def answer(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split(".finance")[0].split("//")[1])
            code = codes.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return TEXT
        tm._get = fake

    def test_url_and_user_agent(self):
        self.assertEqual(tm.chart_url("reliance"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=60d&interval=5m")
        self.assertIn("/chart/M%26M.NS?", tm.chart_url("M&M"))
        self.assertIn("/chart/BAJAJ-AUTO.NS?", tm.chart_url("bajaj-auto", "query1"))
        self.assertEqual(tm.UA, "Mozilla/5.0 (nse-day-trader-skill)")

    def test_429_waits_retries_then_falls_back_to_query1(self):
        self.answer([429, 429, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2", "query1"])
        self.assertEqual(self.sleeps, [tm.BACKOFF, tm.PAUSE])

    def test_one_429_then_ok(self):
        self.answer([429, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2"])

    def test_5xx_falls_back(self):
        self.answer([503, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query1"])

    def test_rate_limited_everywhere_is_one_plain_line(self):
        self.answer([429, 429, 429, 429])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("RELIANCE")
        self.assertIn("rate limiting", str(cm.exception))

    def test_404(self):
        self.answer([404])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("ZZZZ")
        self.assertIn("check the symbol", str(cm.exception))
        self.assertIn("ZZZZ.NS", str(cm.exception))

    def test_run_over_the_network_path(self):
        self.answer([200])
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(PLAN, fh)
            code, text, _ = run(["run", "--plan", p, "--now", NOW, "--replays", "300",
                                 "--config", os.path.join(d, "none.json")])
            self.assertEqual(code, 0)
            self.assertEqual(self.calls, ["query2"])
        finally:
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "nse", "plan.json")
        os.makedirs(os.path.dirname(self.plan))
        with open(self.plan, "w") as fh:
            json.dump(PLAN, fh, indent=2)
        self.out = os.path.join(self.d, "nse", "timemachine.json")
        self.cfg = os.path.join(self.d, "nse", "config.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def base(self, *extra):
        return ["--plan", self.plan, "--config", self.cfg, "--offline", CHART, "--now", NOW] + list(extra)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(["run"] + self.base("--write", self.out))
        self.assertEqual(code, 0, err)
        self.assertIn("target first   15.5 %", text)
        self.assertIn("Leans toward the stop", text)
        self.assertIn("Average R: -0.156 before charges, -0.314 net of charges", text)
        self.assertIn("Wrote %s." % self.out, text)
        self.assertIn("/nse-budget", text)
        with open(self.out) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(k for k in ("plan", "mc", "history") if k in doc), ["history", "mc", "plan"])
        self.assertEqual(doc["created_by"], "nse-timemachine")
        self.assertEqual(doc["source"], "offline")
        for key in ("p_target", "p_stop", "p_neither", "p_green", "avg_R", "avg_r_net", "median_close"):
            self.assertEqual(doc["mc"][key], EXP[key], key)
        self.assertEqual((doc["history"]["triggered"], doc["history"]["avg_R"], doc["history"]["avg_r_net"]),
                         (25, 0.167, -0.013))
        self.assertEqual(doc["plan"]["from"], self.plan)
        self.assertEqual(len(doc["mc"]["sample_paths"]), 160)

    def test_json_output(self):
        code, text, _ = run(["run"] + self.base("--json"))
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["mc"]["p_stop"], EXP["p_stop"])
        self.assertEqual(doc["mc"]["avg_r_net"], EXP["avg_r_net"])
        self.assertNotIn("sample_paths", doc["mc"])
        self.assertNotIn("_exits", doc["mc"])
        self.assertEqual(doc["verdict"], EXP["verdict"])
        self.assertFalse(os.path.exists(self.out))

    def test_the_plan_as_flags_matches_the_file(self):
        code, text, err = run(["run", "--symbol", "reliance", "--date", "2026-10-27", "--entry", "1197.20",
                               "--stop", "1193.30", "--target", "1205.00", "--entry-time", "12:40 PM",
                               "--config", self.cfg, "--offline", CHART, "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_green"]), (EXP["p_target"], EXP["p_green"]))
        self.assertEqual(doc["plan"]["stop_rule"], "or_mid")
        self.assertEqual(doc["mc"]["avg_R"], EXP["avg_R"])
        # no est_charges_per_share in flags: the charges block prices it, still below gross
        self.assertLess(doc["mc"]["avg_r_net"], doc["mc"]["avg_R"])
        code, _, err = run(["run", "--symbol", "RELIANCE", "--entry", "1197.20", "--offline", CHART])
        self.assertEqual(code, 1)
        self.assertIn("--stop", err)

    def test_config_flat_by_and_charges(self):
        with open(self.cfg, "w") as fh:
            json.dump({"flat_by": "14:00", "charges": {k: 0 for k in tm.CHARGES_DEFAULT}}, fh)
        raw = dict(PLAN)
        del raw["flat_by"]
        del raw["est_charges_per_share"]
        with open(self.plan, "w") as fh:
            json.dump(raw, fh)
        code, text, err = run(["run"] + self.base("--json"))
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(doc["mc"]["until"], "14:00")
        self.assertEqual(doc["mc"]["avg_r_net"], doc["mc"]["avg_R"])

    def test_offline_folder_and_default_paths(self):
        shutil.copy(CHART, self.d)
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(os.path.join("nse", "timemachine.json")))
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            self.assertIn("* median", text)
            with open(os.path.join("nse", "timemachine.json")) as fh:
                self.assertEqual(json.load(fh)["mc"]["replays"], 500)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        bad_cfg = os.path.join(self.d, "bad.json")
        with open(bad_cfg, "w") as fh:
            fh.write("{nope")
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     ["run"] + self.base() + ["--offline", os.path.join(self.d, "missing.json.gz")],
                     ["run"] + self.base("--until", "9:00"),
                     ["run"] + self.base("--now", "yesterday"),
                     ["run", "--plan", self.plan, "--config", bad_cfg, "--offline", CHART],
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(run([])[0], 1)

    def test_a_holiday_plan_is_refused_on_the_cli(self):
        raw = dict(PLAN, date="2026-11-10")      # Diwali-Balipratipada
        with open(self.plan, "w") as fh:
            json.dump(raw, fh)
        code, _, err = run(["run"] + self.base())
        self.assertEqual(code, 1)
        self.assertIn("Diwali-Balipratipada", err)

    def test_chart(self):
        run(["run"] + self.base("--write", self.out))
        code, text, _ = run(["chart", "--from", self.out])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertIn("2,000 replay paths from the 12:40 PM IST entry (seed 19)", lines[0])
        self.assertTrue(any(l.startswith("  1205.00 |") and l.endswith("  target") for l in lines))
        self.assertTrue(any(l.startswith("  1193.30 |") and l.endswith("  stop") for l in lines))
        self.assertTrue(any(l.startswith("  1197.20 |*") and l.endswith("  entry") for l in lines))
        self.assertIn("12:40", text)
        self.assertIn("15:10", text)
        self.assertEqual(lines[-1].split(":")[0], "Leans toward the stop")
        code, again, _ = run(["chart"] + self.base())
        self.assertEqual(again, text)


if __name__ == "__main__":
    unittest.main()
