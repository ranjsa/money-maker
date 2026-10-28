import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

scan = load("nse-scanner", "scan")

SDIR = os.path.join(FIX, "nse-scanner")
EQUITY = os.path.join(SDIR, "EQUITY_L.csv")
DEMO = os.path.join(SDIR, "DEMO-5m-synthetic.json")      # hand-built: OR high 1012.40, low 998.10, 09:30 bar 1015.00
EXPECTED = os.path.join(SDIR, "expected.json")
DT = datetime.datetime
D = datetime.date
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def ts(ist_text):
    """IST wall-clock text -> unix seconds."""
    return int(DT.strptime(ist_text, "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp())


def bars_series(day, closes, highs=None, lows=None, vol=100000, start="09:15"):
    """A v8-like series of 5-minute bars from `start` IST on `day`."""
    t0 = DT.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    t = [ts((t0 + datetime.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M")) for i in range(len(closes))]
    return {"t": t, "c": list(closes), "o": None,
            "h": list(highs or [c + 0.1 if c is not None else None for c in closes]),
            "l": list(lows or [c - 0.1 if c is not None else None for c in closes]),
            "v": [vol] * len(closes), "meta": {}}


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class Universe(unittest.TestCase):
    def setUp(self):
        self.text = read(EQUITY)
        self.rows = scan.parse_equity_list(self.text)

    def test_headers_have_leading_spaces_and_are_stripped(self):
        self.assertTrue(self.text.split("\n")[0].count(", ") >= 3)   # the real file's quirk
        r = self.rows[0]
        self.assertEqual(sorted(r), ["isin", "listed", "name", "series", "symbol"])
        self.assertEqual(r["series"], r["series"].strip())
        self.assertTrue(r["isin"].startswith("INE"))

    def test_rows_and_series(self):
        series = {r["series"] for r in self.rows}
        self.assertIn("EQ", series)
        self.assertIn("BE", series)
        by = {r["symbol"]: r for r in self.rows}
        self.assertEqual(by["M&M"]["series"], "EQ")
        self.assertIn("Mahindra", by["M&M"]["name"])

    def test_universe_is_eq_only_sorted_and_keeps_special_symbols(self):
        syms = scan.build_universe(self.text)
        self.assertEqual(syms, sorted(syms))
        self.assertIn("M&M", syms)
        self.assertIn("BAJAJ-AUTO", syms)
        be = [r["symbol"] for r in self.rows if r["series"] == "BE"]
        self.assertTrue(be)
        for s in be:
            self.assertNotIn(s, syms)
        self.assertEqual(len(syms), len([r for r in self.rows if r["series"] == "EQ"]))
        self.assertTrue(30 <= len(self.rows) <= 60)

    def test_not_an_equity_list(self):
        with self.assertRaises(scan.ScanError):
            scan.parse_equity_list("a|b|c\n1|2|3\n")

    def test_universe_command_writes_json_with_names(self):
        d = tempfile.mkdtemp()
        try:
            out = os.path.join(d, "nse", "universe.json")
            code, text, _ = run(["universe", "--offline", SDIR, "--out", out])
            self.assertEqual(code, 0)
            doc = json.loads(read(out))
            self.assertEqual(sorted(doc), ["as_of", "names", "symbols"])
            self.assertIn("M&M", doc["symbols"])
            self.assertEqual(set(doc["names"]), set(doc["symbols"]))
            self.assertIn("Mahindra", doc["names"]["M&M"])
            self.assertRegex(doc["as_of"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+05:30$")
            self.assertIn("EQ", text)
            # the universe file feeds the scans
            self.assertEqual(scan.symbols_from_file(out), doc["symbols"])
        finally:
            shutil.rmtree(d)

    def test_universe_offline_missing_file(self):
        d = tempfile.mkdtemp()
        try:
            code, _, err = run(["universe", "--offline", d, "--no-write"])
            self.assertEqual(code, 1)
            self.assertIn("EQUITY_L.csv", err)
        finally:
            shutil.rmtree(d)


class Network(unittest.TestCase):
    def setUp(self):
        self.paths = []
        self._get, self._sleep = scan.yahoo_get, scan.SLEEP
        self.sleeps = []

        def fake(path):
            self.paths.append(path)
            return json.dumps({"spark": {"result": [], "error": None}})
        scan.yahoo_get = fake
        scan.SLEEP = self.sleeps.append

    def tearDown(self):
        scan.yahoo_get, scan.SLEEP = self._get, self._sleep

    def test_spark_path_encodes_the_ampersand(self):
        p = scan.spark_path(["M&M", "BAJAJ-AUTO", "TCS"], "1d", "5m")
        self.assertIn("symbols=M%26M.NS,BAJAJ-AUTO.NS,TCS.NS", p)
        self.assertIn("range=1d&interval=5m", p)

    def test_chart_path_encodes_the_ampersand(self):
        self.assertTrue(scan.chart_path("M&M", "5d", "5m").startswith("/v8/finance/chart/M%26M.NS?"))

    def test_spark_batches_of_twenty_with_a_pause(self):
        syms = ["S%02d" % i for i in range(45)]
        scan.fetch_spark(syms, "1d", "5m", 0.3)
        self.assertEqual(len(self.paths), 3)
        self.assertEqual(self.paths[0].split("symbols=")[1].split("&")[0].count(","), 19)
        self.assertEqual(self.sleeps, [0.3, 0.3])

    def test_pause_never_under_a_quarter_second(self):
        scan.fetch_spark(["A", "B"] * 15, "1d", "5m", 0.0)
        self.assertEqual(scan.polite(0.0), 0.25)
        self.assertEqual(scan.polite(1.0), 1.0)

    def test_user_agent(self):
        self.assertEqual(scan.UA, "Mozilla/5.0 (nse-day-trader-skill)")

    def test_parse_spark_maps_yahoo_symbols_back(self):
        doc = {"spark": {"result": [
            {"symbol": "M&M.NS", "response": [{"meta": {}, "timestamp": [1], "indicators": {"quote": [{"close": [5.0]}]}}]},
            {"symbol": "BAJAJ-AUTO.NS", "response": [{"meta": {}, "timestamp": [1], "indicators": {"quote": [{"close": [6.0]}]}}]},
        ], "error": None}}
        got = scan.parse_spark(doc)
        self.assertEqual(sorted(got), ["BAJAJ-AUTO", "M&M"])

    def test_ts_to_ist(self):
        self.assertEqual(scan.ts_to_ist(ts("2026-10-27 09:15")), DT(2026, 10, 27, 9, 15))


class OrbPure(unittest.TestCase):
    DAY = D(2026, 10, 27)

    def test_session_bars_start_at_0915_and_drop_the_off_grid_tick(self):
        s = bars_series("2026-10-27", [100, 101, 102, 103, 104, 105])
        s["t"].append(ts("2026-10-27 09:44") + 7)
        for k in ("c", "h", "l"):
            s[k].append(99)
        s["v"].append(1)
        bars = scan.session_bars(s, self.DAY, DT(2026, 10, 27, 9, 45))
        self.assertEqual([b["start"].strftime("%H:%M") for b in bars], ["09:15", "09:20", "09:25", "09:30", "09:35", "09:40"])

    def test_bar_in_progress_is_dropped(self):
        s = bars_series("2026-10-27", [100, 101, 102, 103])
        bars = scan.session_bars(s, self.DAY, DT(2026, 10, 27, 9, 33))
        self.assertEqual(len(bars), 3)

    def test_orb_hit_is_first_close_above_range_high(self):
        closes = [100, 101, 100.5, 101.2, 103]
        highs = [100.5, 102, 101, 101.8, 103.2]
        lows = [99.5, 100, 100, 100.8, 101]
        s = bars_series("2026-10-27", closes, highs, lows)
        bars = scan.session_bars(s, self.DAY, DT(2026, 10, 27, 10, 0))
        h = scan.orb_hit(bars, 3)
        self.assertEqual(h["or_high"], 102.0)
        self.assertEqual(h["or_low"], 99.5)
        self.assertEqual(h["price"], 103.0)
        self.assertEqual(h["bar_start"], "09:35")
        self.assertEqual(h["at"], "09:40")
        self.assertEqual(h["pct_over"], round((103 / 102 - 1) * 100, 2))

    def test_missing_bar_makes_chart_unusable(self):
        s = bars_series("2026-10-27", [100, 101, 102, 103, 104, 105])
        del s["t"][4], s["c"][4], s["h"][4], s["l"][4], s["v"][4]
        bars = scan.session_bars(s, self.DAY, DT(2026, 10, 27, 9, 45))
        self.assertFalse(scan.usable(bars, self.DAY, DT(2026, 10, 27, 9, 45), 3))

    def test_turnover_floor_uses_the_session_bars_so_far(self):
        day, cutoff = self.DAY, DT(2026, 10, 27, 9, 45)
        rich = bars_series("2026-10-27", [100, 101, 100, 103, 104, 105], vol=100000)    # ~6.1 cr
        thin = bars_series("2026-10-27", [100, 101, 100, 103, 104, 105], vol=1000)      # ~0.06 cr
        hits, counts, _ = scan.scan_orb({"RICH": rich, "THIN": thin}, ["RICH", "THIN"], day, cutoff, 15,
                                        min_price=20, min_turnover=50000000)
        self.assertEqual([h["symbol"] for h in hits], ["RICH"])
        self.assertEqual(counts["illiquid"], 1)

    def test_price_floor(self):
        s = bars_series("2026-10-27", [10, 10.1, 10.05, 10.5, 10.6, 10.7], vol=10000000)
        hits, counts, _ = scan.scan_orb({"PENNY": s}, ["PENNY"], self.DAY, DT(2026, 10, 27, 9, 45), 15,
                                        min_price=20, min_turnover=0)
        self.assertEqual(hits, [])
        self.assertEqual(counts["illiquid"], 1)


class Window(unittest.TestCase):
    def now(self, text):
        return scan.parse_now(text)

    def test_holiday_is_named(self):
        with self.assertRaises(scan.ScanError) as c:
            scan.orb_window(self.now("2026-11-10 10:00"), None, 15, None)
        self.assertIn("market closed: Diwali-Balipratipada", str(c.exception))

    def test_weekend(self):
        with self.assertRaises(scan.ScanError) as c:
            scan.orb_window(self.now("2026-10-31 10:00"), None, 15, None)
        self.assertIn("market closed: weekend", str(c.exception))

    def test_range_not_complete(self):
        with self.assertRaises(scan.ScanError) as c:
            scan.orb_window(self.now("2026-10-27 09:25"), None, 15, None)
        self.assertIn("opening range not complete until 09:30 IST", str(c.exception))

    def test_first_bar_after_range(self):
        with self.assertRaises(scan.ScanError) as c:
            scan.orb_window(self.now("2026-10-27 09:32"), None, 15, None)
        self.assertIn("09:35 IST", str(c.exception))

    def test_0935_is_the_first_valid_cutoff(self):
        day, cutoff = scan.orb_window(self.now("2026-10-27 09:35"), None, 15, None)
        self.assertEqual((day, cutoff), (D(2026, 10, 27), DT(2026, 10, 27, 9, 35)))

    def test_after_the_close(self):
        with self.assertRaises(scan.ScanError) as c:
            scan.orb_window(self.now("2026-10-27 16:00"), None, 15, None)
        self.assertIn("closed at 15:30 IST", str(c.exception))

    def test_after_no_entries_after(self):
        with self.assertRaises(scan.ScanError) as c:
            scan.orb_window(self.now("2026-10-27 15:00"), None, 15, datetime.time(14, 45))
        self.assertIn("14:45", str(c.exception))
        self.assertIn("no_entries_after", str(c.exception))

    def test_at_later_than_now(self):
        with self.assertRaises(scan.ScanError) as c:
            scan.orb_window(self.now("2026-10-27 10:00"), datetime.time(11, 0), 15, None)
        self.assertIn("later than now", str(c.exception))

    def test_bad_or_minutes(self):
        with self.assertRaises(scan.ScanError):
            scan.orb_window(self.now("2026-10-27 10:00"), None, 7, None)

    def test_special_session_from_config(self):
        cfg = {"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}
        with self.assertRaises(scan.ScanError) as c:
            scan.orb_window(self.now("2026-11-08 18:10"), None, 15, None, cfg=cfg)
        self.assertNotIn("market closed", str(c.exception))
        self.assertIn("not complete until 18:15 IST", str(c.exception))
        day, cutoff = scan.orb_window(self.now("2026-11-08 18:20"), None, 15, None, cfg=cfg)
        self.assertEqual(cutoff, DT(2026, 11, 8, 18, 20))

    def test_calendar_warning_outside_loaded_years(self):
        self.assertEqual(scan.calendar_warning(D(2026, 10, 27)), "")
        w = scan.calendar_warning(D(2027, 3, 2))
        self.assertIn("NSE calendar for 2027 not loaded", w)
        self.assertIn("nseindia.com", w)
        self.assertIn("nse-alerts/alerts.py calendar --refresh", w)


class DemoOrb(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.out = os.path.join(self.d, "nse", "watchlist.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def orb(self, *extra):
        return run(["orb", "--offline", DEMO, "--out", self.out, "--config", os.path.join(self.d, "none.json")]
                   + list(extra))

    def test_demo_breaks_out_on_the_0930_bar(self):
        code, text, err = self.orb("--now", "2026-10-27 09:35", "--json")
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual([s["symbol"] for s in doc["symbols"]], ["DEMO"])
        s = doc["symbols"][0]
        self.assertEqual(s["or_high"], 1012.40)
        self.assertEqual(s["or_low"], 998.10)
        self.assertEqual(s["close"], 1015.00)
        self.assertEqual(s["pct_over"], 0.26)
        self.assertEqual(s["yahoo"], "DEMO.NS")
        self.assertEqual(s["at"], "09:35")

    def test_at_flag_with_a_pinned_day(self):
        code, text, err = self.orb("--now", "2026-10-27 11:00", "--at", "09:35", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(text)["symbols"][0]["symbol"], "DEMO")

    def test_watchlist_file_shape(self):
        code, text, _ = self.orb("--now", "2026-10-27 10:15")
        self.assertEqual(code, 0)
        doc = json.loads(read(self.out))
        self.assertEqual(doc["created_by"], "nse-scanner")
        self.assertEqual(doc["date"], "2026-10-27")
        self.assertEqual(doc["setup"], "orb")
        self.assertEqual(sorted(doc["symbols"][0])[:3], ["at", "close", "or_high"])
        for k in ("symbol", "yahoo", "or_high", "or_low", "close", "pct_over"):
            self.assertIn(k, doc["symbols"][0])
        # text output is in IST, 24h
        self.assertIn("DEMO", text)
        self.assertIn("IST", text)
        self.assertIn("+0.26%", text)
        self.assertNotIn("$", text)
        self.assertIn("\u20b9", text)

    def test_range_not_complete_refuses_and_writes_nothing(self):
        code, text, err = self.orb("--now", "2026-10-27 09:25")
        self.assertEqual(code, 1)
        self.assertIn("opening range not complete until 09:30 IST", err)
        self.assertFalse(os.path.exists(self.out))

    def test_holiday_refuses_without_fetching(self):
        calls = []
        orig = scan.yahoo_get
        scan.yahoo_get = lambda p: calls.append(p)
        try:
            code, text, err = run(["orb", "--now", "2026-11-10 10:00", "--symbols", "TCS", "--out", self.out,
                                   "--config", os.path.join(self.d, "none.json")])
        finally:
            scan.yahoo_get = orig
        self.assertEqual(code, 1)
        self.assertIn("market closed: Diwali-Balipratipada", text + err)
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(self.out))

    def test_holiday_with_offline_files_also_refuses(self):
        code, text, err = self.orb("--now", "2026-11-10 10:00")
        self.assertEqual(code, 1)
        self.assertIn("market closed: Diwali-Balipratipada", text + err)

    def test_expired_by_config(self):
        cfg = os.path.join(self.d, "config.json")
        with open(cfg, "w") as fh:
            json.dump({"no_entries_after": "10:00"}, fh)
        code, _, err = run(["orb", "--offline", DEMO, "--out", self.out, "--config", cfg, "--now", "2026-10-27 10:15"])
        self.assertEqual(code, 1)
        self.assertIn("no_entries_after", err)

    def test_default_filters_are_inr(self):
        code, text, _ = self.orb("--now", "2026-10-27 10:15", "--json")
        f = json.loads(text)["filters"]
        self.assertEqual(f["min_price"], 20)
        self.assertEqual(f["min_turnover_inr"], 50000000)

    def test_turnover_floor_can_exclude_demo(self):
        code, text, _ = self.orb("--now", "2026-10-27 10:15", "--min-turnover-inr", "100000000000", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)["symbols"], [])

    def test_offline_without_at_needs_a_time(self):
        code, _, err = run(["orb", "--offline", DEMO, "--out", self.out])
        self.assertEqual(code, 1)
        self.assertIn("--at", err)


def daily_series(closes, last_day=D(2026, 10, 27), vol=1000000, price=None):
    """Daily bars stamped 09:15 IST on consecutive weekdays ending at last_day."""
    days, d = [], last_day
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= datetime.timedelta(days=1)
    days.reverse()
    return {"t": [ts("%s 09:15" % x.isoformat()) for x in days], "c": list(closes), "o": None, "h": None, "l": None,
            "v": None, "meta": {"regularMarketPrice": price if price is not None else closes[-1],
                                "regularMarketVolume": vol}}


class Daily(unittest.TestCase):
    NOW = scan.parse_now("2026-10-28 08:00")

    def test_breakout_20d(self):
        closes = [100 + (i % 3) for i in range(40)] + [110]
        hits, counts, last = scan.scan_daily({"X": daily_series(closes)}, ["X"], self.NOW, 20, 50000000)
        self.assertEqual(hits[0]["setups"][0], "breakout_20d")
        self.assertEqual(last, D(2026, 10, 27))
        self.assertEqual(counts["scanned"], 1)

    def test_oversold(self):
        closes = [200 - 2 * i for i in range(40)]
        hits, _, _ = scan.scan_daily({"X": daily_series(closes)}, ["X"], self.NOW, 20, 50000000)
        self.assertIn("oversold", hits[0]["setups"])

    def test_turnover_is_price_times_session_volume(self):
        closes = [100 + (i % 3) for i in range(40)] + [110]
        s = daily_series(closes, vol=1000)                     # 110 x 1,000 = Rs 1.1 lakh
        hits, counts, _ = scan.scan_daily({"X": s}, ["X"], self.NOW, 20, 50000000)
        self.assertEqual(hits, [])
        self.assertEqual(counts["illiquid"], 1)
        s = daily_series(closes, vol=1000000)                  # 110 x 10,00,000 = Rs 11 cr
        hits, _, _ = scan.scan_daily({"X": s}, ["X"], self.NOW, 20, 50000000)
        self.assertEqual(len(hits), 1)

    def test_price_floor(self):
        closes = [10 + (i % 3) * 0.1 for i in range(40)] + [12]
        hits, counts, _ = scan.scan_daily({"X": daily_series(closes, vol=100000000)}, ["X"], self.NOW, 20, 0)
        self.assertEqual(hits, [])
        self.assertEqual(counts["illiquid"], 1)

    def test_todays_bar_waits_for_the_close(self):
        closes = [100 + (i % 3) for i in range(40)] + [110]
        s = daily_series(closes, last_day=D(2026, 10, 28))
        before = scan.daily_closes(s, scan.parse_now("2026-10-28 12:00"))
        after = scan.daily_closes(s, scan.parse_now("2026-10-28 15:30"))
        self.assertEqual(before[-1][0], D(2026, 10, 27))
        self.assertEqual(after[-1][0], D(2026, 10, 28))

    def test_daily_command_writes_watchlist(self):
        d = tempfile.mkdtemp()
        try:
            closes = [100 + (i % 3) for i in range(40)] + [110]
            path = os.path.join(d, "X.json")
            res = {"meta": {"regularMarketPrice": 110, "regularMarketVolume": 1000000, "symbol": "X.NS"},
                   "timestamp": daily_series(closes)["t"], "indicators": {"quote": [{"close": closes}]}}
            with open(path, "w") as fh:
                json.dump({"spark": {"result": [{"symbol": "X.NS", "response": [res]}], "error": None}}, fh)
            out = os.path.join(d, "nse", "watchlist.json")
            code, text, err = run(["daily", "--offline", path, "--now", "2026-10-28 08:00", "--out", out])
            self.assertEqual(code, 0, err)
            doc = json.loads(read(out))
            self.assertEqual(doc["created_by"], "nse-scanner")
            self.assertEqual(doc["setup"], "daily")
            self.assertEqual(doc["symbols"][0]["symbol"], "X")
            self.assertEqual(doc["symbols"][0]["yahoo"], "X.NS")
            self.assertIn("level", doc["symbols"][0])
            self.assertEqual(doc["date"], "2026-10-28")
            self.assertIn("\u20b9", text)
        finally:
            shutil.rmtree(d)


class RealFixtures(unittest.TestCase):
    """Real Yahoo charts captured by tests/capture.py; expected.json is what the tool said about them then."""

    def setUp(self):
        self.exp = json.loads(read(EXPECTED))
        self.day = self.exp["day"]

    def fixture(self, stem):
        p = os.path.join(SDIR, "%s-%s.json" % (stem, self.day))
        return p if os.path.exists(p) else p + ".gz"

    def test_orb_count_matches_expected(self):
        code, text, err = run(["orb", "--offline", self.fixture("chart-5m"), "--now", "%s %s" % (self.day, self.exp["at"]),
                               "--json", "--no-write"])
        doc = json.loads(text)
        self.assertEqual(len(doc["symbols"]), self.exp["orb_hits"])
        self.assertEqual([s["symbol"] for s in doc["symbols"]], self.exp["orb_symbols"])
        self.assertEqual(doc["scanned"], self.exp["orb_scanned"])
        self.assertEqual(code, 0 if self.exp["orb_hits"] else 1)
        self.assertGreater(doc["scanned"], 20)

    def test_special_symbols_survive_the_trip(self):
        data, _ = scan.load_offline(self.fixture("chart-5m"))
        self.assertIn("M&M", data)
        self.assertIn("BAJAJ-AUTO", data)

    def test_daily_count_matches_expected(self):
        code, text, _ = run(["daily", "--offline", self.fixture("daily-spark"), "--now", "%s 08:00" % (
            D.fromisoformat(self.day) + datetime.timedelta(days=1)), "--json", "--no-write"])
        doc = json.loads(text)
        self.assertEqual(sorted(s["symbol"] for s in doc["symbols"]), self.exp["daily_symbols"])

    def test_spark_fixture_is_closes_only_and_parses(self):
        data, errs = scan.load_offline(self.fixture("spark-5m"))
        self.assertIn("M&M", data)
        self.assertIsNone(data["M&M"]["h"])


class Capture(unittest.TestCase):
    def test_capture_registry_and_gzip_rule(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("capture", os.path.join(os.path.dirname(__file__), "capture.py"))
        cap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cap)
        self.assertIn("nse-scanner", cap.REGISTRY)
        urls = [u for u, _ in cap.REGISTRY["nse-scanner"]["files"]]
        self.assertIn("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv", urls)
        d = tempfile.mkdtemp()
        try:
            small, _ = cap.write_fixture(d, "a.json", "{}")
            big, _ = cap.write_fixture(d, "b.json", "x" * (201 * 1024))
            self.assertTrue(small.endswith("a.json"))
            self.assertTrue(big.endswith("b.json.gz"))
            with gzip.open(big, "rt") as fh:
                self.assertEqual(len(fh.read()), 201 * 1024)
        finally:
            shutil.rmtree(d)


class Repo(unittest.TestCase):
    def test_skill_md_is_nse(self):
        text = read(os.path.join(os.path.dirname(__file__), "..", "nse-scanner", "SKILL.md"))
        for w in ("scan NSE", "Nifty breakouts", "nse/watchlist.json", "IST"):
            self.assertIn(w, text)
        self.assertIn("not financial advice", text.lower())
        self.assertNotIn("\u2014", text)


if __name__ == "__main__":
    unittest.main()
