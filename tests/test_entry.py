"""nse-entry tests. Fixtures in tests/fixtures/nse-entry/:

DEMO-5m-synthetic.json   SYNTHETIC five-minute bars (not market data) for Tuesday 2026-10-27: the
                         09:15, 09:20 and 09:25 bars set the opening range (high 1012.40, low 998.10)
                         and the 09:30 bar closes 1015.00, so the trigger is the 09:35 AM IST call.
DEMO-1m-synthetic.json   SYNTHETIC one-minute bars: a wick above the range high, a close AT the high,
                         a null bar and Yahoo's live tick at 09:37:30.
RELIANCE-5m-2026-10-27.json  RELIANCE.NS's real 2026-10-27 five-minute bars (Yahoo chart JSON v8),
                         captured with tests/capture.py nse-entry. expected.json holds what the tool
                         said about them when they were captured.
"""

import copy
import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

entry = load("nse-entry", "entry")

EDIR = os.path.join(FIX, "nse-entry")
DEMO = os.path.join(EDIR, "DEMO-5m-synthetic.json")
DEMO1 = os.path.join(EDIR, "DEMO-1m-synthetic.json")
RELI = os.path.join(EDIR, "RELIANCE-5m-2026-10-27.json")
OPEN_TS = 1793072700       # 2026-10-27 09:15 IST
DT = datetime.datetime
UTC = datetime.timezone.utc


def ist(text):
    return entry.parse_now(text)


def doc(path=DEMO):
    with open(path) as fh:
        return json.load(fh)


def chart(d=None, sym="DEMO"):
    return entry.parse_chart(json.dumps(d if d is not None else doc()), sym)


def at(text, d=None, **kw):
    return entry.evaluate(chart(d), ist(text), **kw)


def no_breakout(d=None):
    """Every bar after the range stays under the 1012.40 high."""
    d = copy.deepcopy(d if d is not None else doc())
    q = d["chart"]["result"][0]["indicators"]["quote"][0]
    for i in range(3, len(q["close"])):
        q["close"][i] = min(q["close"][i], 1010.0)
        q["high"][i] = min(q["high"][i], 1011.0)
        q["open"][i] = min(q["open"][i], 1010.0)
    return d


def late_breakout(d=None):
    """The 09:30 bar closes under the high, so the first close above is the 09:35 bar's (1015.30)."""
    d = copy.deepcopy(d if d is not None else doc())
    q = d["chart"]["result"][0]["indicators"]["quote"][0]
    q["close"][3], q["high"][3] = 1010.0, 1011.0
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class DemoDay(unittest.TestCase):
    def test_09_35_triggers_on_the_0930_bar(self):
        r = at("2026-10-27 09:35")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["opening_range"]["high"], 1012.4)
        self.assertEqual(r["opening_range"]["low"], 998.1)
        self.assertEqual(r["opening_range"]["bars"], 3)
        t = r["trigger"]
        self.assertEqual(t["time"], "09:35 AM")
        self.assertEqual(t["bar_start"], "09:30")
        self.assertEqual(t["bar_index"], 3)
        self.assertEqual(t["price"], 1015.0)
        self.assertEqual(t["pct_over"], 0.26)
        self.assertEqual(r["level"], 1012.4)
        self.assertEqual(r["prev_close"], 995.0)
        self.assertEqual(r["date"], "2026-10-27")
        self.assertEqual(r["now"], "2026-10-27 09:35:00")

    def test_09_33_the_0930_bar_is_still_forming(self):
        r = at("2026-10-27 09:33")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"]["bar_start"], "09:25")
        self.assertEqual(r["last"]["close"], 1006.0)
        self.assertEqual(r["forming"]["bar_start"], "09:30")
        self.assertIn("under the range high", r["reason"])
        self.assertIn("₹6.40", r["reason"])
        self.assertEqual(at("2026-10-27 09:34:59")["status"], "waiting")

    def test_before_the_open_and_while_the_range_forms(self):
        r = at("2026-10-27 09:05")
        self.assertEqual((r["status"], r["phase"]), ("not_open", "pre-open"))
        self.assertIn("opens 09:15 AM IST", "\n".join(entry.describe(r)))
        r = at("2026-10-27 09:29")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-10-27 09:31")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)

    def test_after_the_close_it_stays_triggered_and_says_it_is_late(self):
        r = at("2026-10-27 16:05")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["trigger"]["time"], "09:35 AM")
        text = "\n".join(entry.describe(r))
        self.assertIn("minutes ago", text)
        self.assertNotIn("This is the moment", text)

    def test_describe_in_ist_and_rupees(self):
        text = "\n".join(entry.describe(at("2026-10-27 09:35")))
        self.assertIn("Tue 2026-10-27 09:35 IST", text)
        self.assertIn("TRIGGERED 09:35 AM at ₹1,015.00", text)
        self.assertIn("(bar 3)", text)
        self.assertIn("/nse-exit", text)
        self.assertIn("Nothing was placed", text)
        for never in ("ET", "New " + "York", "$"):
            self.assertNotIn(never, text)


class Rules(unittest.TestCase):
    def test_volume_confirmation_moves_the_trigger(self):
        r = at("2026-10-27 09:50", min_volume_x=1.05)
        self.assertEqual(r["opening_range"]["avg_volume"], 30500)
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_index"], t["price"]), ("09:45 AM", 5, 1014.2))
        self.assertEqual(t["volume_x"], 1.07)
        r = at("2026-10-27 09:35", min_volume_x=1.05)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("1.03x the range's volume, under your 1.05x", r["reason"])

    def test_level_setup(self):
        r = at("2026-10-27 09:45", setup="level", level=1014)
        self.assertEqual(r["trigger"]["time"], "09:35 AM")
        self.assertEqual(r["trigger"]["bar_index"], 3)
        self.assertEqual(r["trigger"]["price"], 1015.0)
        # a level under the open: the first eligible close is the 09:25 bar's, at 09:30 (no_entries_before)
        r = at("2026-10-27 09:45", setup="level", level=1000)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("09:30 AM", 2))
        with self.assertRaises(entry.EntryError):
            at("2026-10-27 09:45", setup="level")

    def test_no_breakout_waits_then_expires_at_1445(self):
        d = no_breakout()
        r = at("2026-10-27 09:50", d)
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(at("2026-10-27 14:44", d)["status"], "waiting")
        r = at("2026-10-27 14:45", d)
        self.assertEqual(r["status"], "expired")
        self.assertIn("by 14:45", r["reason"])
        self.assertEqual(at("2026-10-27 15:40", d)["phase"], "after-close")
        self.assertEqual(r["no_entries_after"], "14:45")
        self.assertEqual(r["no_entries_before"], "09:30")

    def test_the_bar_closing_at_the_cutoff_still_counts_when_yahoo_lags(self):
        d = no_breakout()
        r0 = d["chart"]["result"][0]
        keep = [i for i, t in enumerate(r0["timestamp"]) if t <= OPEN_TS + 5 * 300]      # live: up to the 09:40 bar
        r0["timestamp"] = [r0["timestamp"][i] for i in keep]
        q = r0["indicators"]["quote"][0]
        for k in q:
            q[k] = [q[k][i] for i in keep]
        r0["meta"]["regularMarketTime"] = OPEN_TS + 5 * 300 + 280                      # last trade 09:44:40
        cfg = {"no_entries_before": "09:30", "no_entries_after": "09:45"}
        r = at("2026-10-27 09:45:10", d, cfg=cfg)                          # the 09:40 bar closed by the clock only
        self.assertEqual(r["status"], "waiting")
        self.assertIn("Yahoo's last trade is 09:44:40", r["lag_note"])
        self.assertEqual(at("2026-10-27 09:46", d, cfg=cfg)["status"], "expired")

    def test_config_cutoff_and_earliest(self):
        cfg = {"no_entries_before": "09:30", "no_entries_after": "09:30"}
        self.assertEqual(at("2026-10-27 09:35", cfg=cfg)["status"], "expired")
        cfg = {"no_entries_before": "09:40", "no_entries_after": "14:45"}
        r = at("2026-10-27 09:50", cfg=cfg)
        self.assertEqual(r["trigger"]["time"], "09:40 AM")        # the 09:35 bar, first to close at or after 09:40

    def test_bad_inputs(self):
        with self.assertRaises(entry.EntryError):
            at("2026-10-27 09:35", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-10-27 09:35", interval="15m")
        with self.assertRaises(ValueError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")
        self.assertEqual(ist("2026-10-27 09:35"), DT(2026, 10, 27, 4, 5, tzinfo=UTC))
        self.assertEqual(ist("2026-10-27T04:05:00Z"), DT(2026, 10, 27, 4, 5, tzinfo=UTC))
        self.assertEqual(ist("2026-10-27 09:35 IST"), DT(2026, 10, 27, 4, 5, tzinfo=UTC))

    def test_or_minutes_30(self):
        r = at("2026-10-27 10:00", or_minutes=30)
        self.assertEqual(r["opening_range"]["bars"], 6)
        self.assertEqual(r["opening_range"]["high"], 1016.2)
        self.assertEqual((r["status"], r["level"]), ("waiting", 1016.2))     # nothing closes above 1016.20 after 09:45


class OneMinute(unittest.TestCase):
    def c(self):
        return entry.parse_chart(json.dumps(doc(DEMO1)), "DEMO")

    def test_wick_and_close_at_the_high_do_not_trigger(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m")
        self.assertEqual(r["now"], "2026-10-27 09:37:30")
        self.assertEqual(r["opening_range"], {"high": 1005.0, "low": 990.0, "minutes": 15, "bars": 15, "of": 15,
                                              "complete": True, "avg_volume": 10000})
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("09:34 AM", "09:33", 18, 1005.6))
        r = entry.evaluate(c, ist("2026-10-27 09:33:59"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "09:33")
        self.assertEqual(r["last"]["close"], 1005.0)             # the 09:32 bar closed AT the high

    def test_null_bar_tick_and_volume(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m", min_volume_x=2)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("09:37 AM", 21))
        slots = [b["slot"] for b in r["_bars"]]
        self.assertNotIn(20, slots)                               # 09:35 came as nulls
        self.assertEqual(slots[-1], 21)                           # 09:37 still forming, 09:37:30 tick dropped
        self.assertEqual(r["forming"]["bar_start"], "09:37")

    def test_yahoo_lag_on_the_last_bar(self):
        c = self.c()
        r = entry.evaluate(c, ist("2026-10-27 09:38"), interval="1m")
        self.assertEqual(r["_bars"][-1]["slot"], 21)
        self.assertIn("Yahoo's last trade is 09:37:30", r["lag_note"])
        r = entry.evaluate(c, ist("2026-10-27 09:39"), interval="1m")
        self.assertEqual(r["_bars"][-1]["slot"], 22)
        self.assertTrue(all(b["t"] % 60 == 0 for b in r["_bars"]))

    def test_the_file_and_the_interval_must_agree(self):
        code, _, err = run(["check", "DEMO", "--offline", DEMO1])
        self.assertEqual(code, 1)
        self.assertIn("1m bars, not 5m", err)


class Calendar(unittest.TestCase):
    def test_diwali_balipratipada_is_closed(self):
        r = at("2026-11-10 10:00")
        self.assertEqual((r["status"], r["phase"]), ("closed", "closed"))
        self.assertIn("Diwali-Balipratipada", r["reason"])
        self.assertEqual(r["next_open"], "2026-11-11T09:15+05:30")
        text = "\n".join(entry.describe(r))
        self.assertIn("MARKET CLOSED", text)
        self.assertIn("Next open: 2026-11-11 09:15 IST", text)

    def test_weekend_and_empty_data(self):
        r = at("2026-10-31 10:30")
        self.assertEqual((r["status"], r["phase"]), ("closed", "closed"))
        self.assertIn("weekend", r["reason"])
        self.assertEqual(r["next_open"], "2026-11-02T09:15+05:30")
        empty = copy.deepcopy(doc())
        res = empty["chart"]["result"][0]
        del res["timestamp"]
        res["indicators"] = {"quote": [{}]}
        r = at("2026-10-27 10:15", empty)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "open"))
        r = at("2026-10-28 10:15")                                # Yahoo sent yesterday's bars only
        self.assertEqual(r["status"], "no-data")

    def test_a_year_outside_the_calendar_warns_and_treats_weekdays_as_open(self):
        r = at("2027-01-04 10:00")
        self.assertFalse(r["calendar_known"])
        self.assertNotEqual(r["status"], "closed")
        text = "\n".join(entry.describe(r))
        self.assertIn("NSE calendar for 2027 not loaded: run nse-alerts/alerts.py calendar --refresh or check "
                      "nseindia.com (treating Mon-Fri as open)", text)
        self.assertEqual(entry.calendar_warning(datetime.date(2026, 10, 27)), "")
        self.assertEqual(at("2027-01-02 10:00")["status"], "closed")        # a Saturday is still a weekend

    def test_a_special_session_from_config(self):
        cfg = {"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}
        r = at("2026-11-08 17:00", cfg=cfg)
        self.assertEqual(r["status"], "not_open")
        self.assertIn("opens 06:00 PM IST", "\n".join(entry.describe(r)))
        self.assertEqual(at("2026-11-08 17:00")["status"], "closed")       # no config entry: a Sunday

    def test_yahoo_errors(self):
        bad = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                              "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_chart(bad, "ZZZZ")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_chart("<html>", "RELIANCE")


class Network(unittest.TestCase):
    """Yahoo is never called: _open_url and _sleep are swapped for fakes."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._open, self._sleep = entry._open_url, entry._sleep
        entry._sleep = lambda s: self.sleeps.append(s)
        with open(DEMO) as fh:
            self.body = fh.read()
        self.day = datetime.date(2026, 10, 27)

    def tearDown(self):
        entry._open_url, entry._sleep = self._open, self._sleep

    def answers(self, *codes):
        seq = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url)
            code = seq.pop(0)
            if code == 200:
                return self.body
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b'{"chart":{"result":null,'
                                                                           b'"error":{"description":"No data found"}}}'))
        entry._open_url = fake

    def test_ua(self):
        self.assertEqual(entry.UA, "Mozilla/5.0 (nse-day-trader-skill)")

    def test_url_and_429_retry_once_after_a_pause(self):
        self.answers(429, 200)
        c = entry.get_chart("DEMO", "5m", self.day, today=self.day)
        self.assertEqual(len(c["bars"]), 75)
        self.assertEqual(c["symbol"], "DEMO")
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com/v8/finance/chart/DEMO.NS?range=1d&interval=5m"] * 2)
        self.assertEqual(self.sleeps, [entry.BACKOFF])

    def test_429_twice_falls_back_to_query1(self):
        self.answers(429, 429, 200)
        entry.get_chart("DEMO", "5m", self.day, today=self.day)
        self.assertEqual([u.split("/")[2] for u in self.calls],
                         ["query2.finance.yahoo.com", "query2.finance.yahoo.com", "query1.finance.yahoo.com"])

    def test_5xx_falls_back_without_waiting(self):
        self.answers(503, 200)
        entry.get_chart("DEMO", "5m", self.day, today=self.day)
        self.assertEqual(self.sleeps, [])
        self.assertIn("query1", self.calls[1])

    def test_still_busy_is_one_plain_error(self):
        self.answers(429, 429, 429, 429)
        code, out, err = run(["check", "DEMO", "--now", "2026-10-27 09:35"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_ticker_404(self):
        self.answers(404)
        with self.assertRaises(entry.EntryError) as cm:
            entry.get_chart("ZZZZ", "5m", self.day, today=self.day)
        self.assertIn("No data found", str(cm.exception))

    def test_a_closed_day_never_calls_yahoo(self):
        self.answers()
        code, text, _ = run(["check", "RELIANCE", "--now", "2026-11-10 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("Diwali-Balipratipada", text)
        self.assertEqual(self.calls, [])

    def test_ranges_for_past_sessions(self):
        today = datetime.date(2026, 10, 28)
        self.assertEqual(entry.chart_range(today, today, "5m"), "1d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 27), today, "5m"), "5d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 12), today, "5m"), "1mo")
        self.assertEqual(entry.chart_range(datetime.date(2026, 9, 1), today, "5m"), "60d")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(datetime.date(2026, 10, 12), today, "1m")

    def test_symbols_with_ampersand_and_hyphen(self):
        self.assertIn("/chart/M%26M.NS?", entry.chart_path("m&m", "1d", "5m"))
        self.assertIn("/chart/BAJAJ-AUTO.NS?", entry.chart_path("bajaj-auto", "1d", "5m"))
        self.assertIn("/chart/J%26KBANK.NS?", entry.chart_path("J&KBANK", "1d", "5m"))


def live_provider(d, lag_until=None, calls=None, spike=1013.0):
    """Bars the way Yahoo serves them at `now`: closed bars final, the in-progress bar
    showing a spike above the range high (it may close under), plus a live tick.
    lag_until: before this UTC time Yahoo's last trade lags 30 seconds behind."""
    base = chart(d)

    def provider(now):
        if calls is not None:
            calls.append(now)
        nts = entry.ts_of(now)
        lagging = lag_until is not None and now < lag_until
        data_ts = nts - 30 if lagging else nts
        bars = []
        for b in base["bars"]:
            if b["t"] % 300 or b["t"] > data_ts:
                continue
            b = dict(b)
            if b["t"] + 300 > data_ts:
                b["c"], b["h"] = spike, max(b["h"], spike)
            bars.append(b)
        if data_ts % 300:
            bars.append({"t": data_ts, "o": spike, "h": spike, "l": spike, "c": spike, "v": 0})
        return {"symbol": "DEMO", "meta": {}, "bars": bars, "prev_close": 995.0, "data_time": data_ts}
    return provider


def hhmmss(utc):
    return entry.to_ist(utc).strftime("%H:%M:%S")


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def loop(self, provider, start, **kw):
        clock = entry.SimClock(ist(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, dict(self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(late_breakout(), calls=calls), "2026-10-27 09:33",
                                                   every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0]["trigger"]["time"], "09:40 AM")
        self.assertEqual(fired[0][0]["trigger"]["price"], 1015.3)
        # it looked at 09:33 (09:30 bar spiking to 1013 in progress) and at 09:35:02 (that bar closed 1010)
        self.assertEqual([hhmmss(c) for c in calls], ["09:33:00", "09:35:02", "09:40:02"])
        text = "\n".join(lines)
        self.assertIn("09:30 bar  closed ₹1,010.00  ₹2.40 under ₹1,012.40", text)
        self.assertIn("09:35 bar  closed ₹1,015.30  TRIGGERED", text)
        self.assertEqual(text.count("TRIGGERED"), 1)

    def test_waits_for_yahoo_to_catch_up(self):
        lag = ist("2026-10-27 09:40:30")
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(late_breakout(), lag_until=lag, calls=calls),
                                                   "2026-10-27 09:38", every=30)
        self.assertEqual(code, 0)
        self.assertEqual([hhmmss(c) for c in calls], ["09:38:00", "09:40:02", "09:40:32"])   # 09:40:02: Yahoo at 09:39:32
        self.assertEqual(fired[0][0]["trigger"]["time"], "09:40 AM")

    def test_from_before_the_open_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(late_breakout()), "2026-10-27 09:00", every=30)
        self.assertEqual(code, 0)
        text = "\n".join(lines)
        self.assertIn("not open yet", lines[0])
        self.assertIn("09:25 bar  closed ₹1,006.00  opening range set: high ₹1,012.40, low ₹998.10", text)
        self.assertEqual(sum(1 for x in lines if " bar  closed " in x), 5)    # 09:15 ... 09:35, one line each

    def test_a_computer_that_dozes_off_catches_up_when_it_wakes(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 3:                      # the laptop sleeps for 17 minutes mid-wait
                    self.t += datetime.timedelta(minutes=17)
        clock = Dozing(ist("2026-10-27 09:21"))
        calls, lines = [], []
        code, res = entry.watch_loop(live_provider(late_breakout(), calls=calls), clock.now, clock.sleep,
                                     lines.append, dict(self.OPTS), every=30)
        self.assertEqual(code, 0)
        self.assertEqual([hhmmss(c) for c in calls][:2], ["09:21:00", "09:39:30"])   # straight after waking
        text = "\n".join(lines)
        for start in ("09:20", "09:25", "09:30"):
            self.assertIn(start + " bar  closed", text)          # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "09:40 AM")

    def test_expires_and_until(self):
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-10-27 14:00", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(fired, [])
        self.assertEqual(entry.to_ist(clock.now()).strftime("%H:%M"), "14:45")
        until = DT(2026, 10, 27, 11, 0)
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-10-27 10:30", every=300,
                                                   until=until)
        self.assertEqual(code, 1)
        self.assertIn("--until 11:00", lines[-1])

    def test_closed_day_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-10-31 10:00")
        self.assertEqual(code, 1)
        self.assertIn("weekend", lines[0])
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-11-10 10:00")
        self.assertEqual(code, 1)
        self.assertIn("Diwali-Balipratipada", lines[0])

        def broken(now):
            raise entry.EntryError("Yahoo did not answer")
        code, _, lines, _, _ = self.loop(broken, "2026-10-27 10:00", every=30)
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "DEMO", "--offline", DEMO, "--now", "2026-10-27 09:35", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 09:35 AM at ₹1,015.00", text)
        self.assertIn("/nse-exit", text)
        with open(os.path.join("nse", "entry.json")) as fh:
            e = json.load(fh)
        want = {"created_by": "nse-entry", "symbol": "DEMO", "setup": "orb", "side": "buy", "date": "2026-10-27",
                "status": "triggered", "time": "09:35 AM", "bar_start": "09:30", "bar_index": 3, "price": 1015.0,
                "level": 1012.4, "interval": "5m", "opening_range": {"high": 1012.4, "low": 998.1, "minutes": 15},
                "exchange": "NSE", "source": "yahoo", "checked_at": "2026-10-27T09:35:00+05:30"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "DEMO", "--offline", DEMO, "--now", "2026-10-27 09:33", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)
        code, text, _ = run(["check", "DEMO", "--offline", DEMO, "--now", "2026-10-27 09:33", "--write", "e.json"])
        with open("e.json") as fh:
            self.assertEqual(json.load(fh)["status"], "waiting")
        self.assertIn("still forming", text)

    def test_closed_and_not_open_runs(self):
        code, text, _ = run(["check", "DEMO", "--offline", DEMO, "--now", "2026-11-10 10:00", "--write"])
        self.assertEqual(code, 1)
        self.assertIn("Diwali-Balipratipada", text)
        with open(os.path.join("nse", "entry.json")) as fh:
            e = json.load(fh)
        self.assertEqual(e["status"], "closed")
        self.assertIsNone(e["time"])
        code, text, _ = run(["check", "DEMO", "--offline", DEMO, "--now", "2026-10-27 09:05"])
        self.assertEqual(code, 1)
        self.assertIn("opens 09:15 AM IST", text)
        code, text, err = run(["check", "DEMO", "--offline", DEMO, "--now", "2027-01-04 10:00"])
        self.assertIn("NSE calendar for 2027 not loaded", text + err)

    def test_offline_gz_folder_and_mismatch(self):
        os.mkdir("charts")
        with open(DEMO, "rb") as src, gzip.open(os.path.join("charts", "DEMO.json.gz"), "wb") as dst:
            dst.write(src.read())
        code, text, _ = run(["check", "demo", "--offline", "charts", "--now", "2026-10-27 09:35"])
        self.assertEqual(code, 0)
        code, _, err = run(["check", "INFY", "--offline", DEMO])
        self.assertEqual(code, 1)
        self.assertIn("holds DEMO, not INFY", err)

    def test_longs_only_and_flag_errors(self):
        code, _, err = run(["check", "DEMO", "--offline", DEMO, "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("v1 plans longs only", err)
        code, _, err = run(["check", "DEMO", "--offline", DEMO, "--level", "300"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "DEMO", "--offline", DEMO, "--setup", "level"])
        self.assertIn("needs --level", err)
        code, _, err = run(["check", "DEMO", "--offline", DEMO, "--now", "tomorrow"])
        self.assertEqual(code, 1)
        self.assertIn("IST", err)

    def test_config_is_read(self):
        os.mkdir("nse")
        with open(os.path.join("nse", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "09:30"}, fh)
        code, text, _ = run(["check", "DEMO", "--offline", DEMO, "--now", "2026-10-27 09:35"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)
        with open(os.path.join("nse", "config.json"), "w") as fh:
            json.dump({"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}, fh)
        code, text, _ = run(["check", "DEMO", "--offline", DEMO, "--now", "2026-11-08 17:00"])
        self.assertIn("opens 06:00 PM IST", text)
        with open(os.path.join("nse", "config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "DEMO", "--offline", DEMO])
        self.assertIn("not valid JSON", err)

    def test_watch_replays_the_file_and_writes_the_entry(self):
        with open("late.json", "w") as fh:
            json.dump(late_breakout(), fh)
        code, text, _ = run(["watch", "DEMO", "--offline", "late.json", "--now", "2026-10-27 09:20", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn("09:35 bar  closed ₹1,015.30  TRIGGERED", text)
        with open(os.path.join("nse", "entry.json")) as fh:
            self.assertEqual(json.load(fh)["bar_index"], 4)
        code, text, _ = run(["watch", "DEMO", "--offline", "late.json", "--now", "2026-10-27 09:20", "--until", "09:30",
                             "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("x.json"))

    def test_watchlist(self):
        os.mkdir("charts")
        shutil.copy(DEMO, os.path.join("charts", "DEMO.json"))
        shutil.copy(RELI, os.path.join("charts", "RELIANCE.json"))
        os.mkdir("nse")
        with open(os.path.join("nse", "watchlist.json"), "w") as fh:
            json.dump({"created_by": "nse-scanner", "date": "2026-10-27", "setup": "orb", "symbols": [
                {"symbol": "DEMO", "yahoo": "DEMO.NS", "close": 1015.0, "setups": ["orb"], "or_high": 1012.4,
                 "or_low": 998.1},
                {"symbol": "ZZZZ", "yahoo": "ZZZZ.NS", "close": 10.0, "setups": ["orb"]},
                {"symbol": "RELIANCE", "yahoo": "RELIANCE.NS", "close": 1194.0, "setups": ["orb"]}]}, fh)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-27 09:35"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertTrue(rows[1].startswith("DEMO        triggered  09:35 AM  ₹1,015.00"), rows[1])
        self.assertIn("ZZZZ        error: no offline file", rows[2])
        self.assertTrue(rows[3].startswith("RELIANCE    waiting"), rows[3])
        self.assertIn("1 of 3 triggered", text)
        self.assertIn("/nse-exit", text)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-27 09:33", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)[0]["result"]["status"], "waiting")
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("run /nse-scanner first", err)
        with open(os.path.join("nse", "watchlist.json"), "w") as fh:
            json.dump({"symbols": []}, fh)
        code, _, err = run(["watchlist"])
        self.assertIn("no symbols in", err)


class Source(unittest.TestCase):
    def test_offline_file_is_defined_once(self):
        # M4: a second copy of offline_file silently shadowed the first
        import ast
        with open(os.path.join(ROOT, "nse-entry", "entry.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        self.assertEqual([n for n in set(names) if names.count(n) > 1], [])


class RealData(unittest.TestCase):
    """RELIANCE.NS's real 2026-10-27 session, against what the tool said when it was captured."""

    def test_reliance_matches_expected_json(self):
        with open(os.path.join(EDIR, "expected.json")) as fh:
            exp = json.load(fh)
        for when, want in exp["checks"].items():
            code, text, _ = run(["check", "RELIANCE", "--offline", RELI, "--now", "%s %s" % (exp["day"], when), "--json"])
            r = json.loads(text)
            self.assertEqual(r["status"], want["status"], when)
            self.assertEqual(r["level"], want["level"], when)
            self.assertEqual(r["opening_range"]["high"], exp["or_high"], when)
            self.assertEqual(r["opening_range"]["low"], exp["or_low"], when)
            t = r["trigger"] or {}
            self.assertEqual(t.get("time"), want["trigger_time"], when)
            self.assertEqual(t.get("price"), want["trigger_price"], when)
            self.assertEqual(code, 0 if want["status"] == "triggered" else 1, when)
        self.assertEqual(exp["symbol"], "RELIANCE")


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "nse-entry", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "nse-scanner", "SKILL.md"), encoding="utf-8") as fh:
            ref = fh.read()
        self.assertTrue(text.startswith("---\nname: nse-entry\n"))
        self.assertGreater(len(text.splitlines()), 60)
        fm = text.split("\n---\n")[0]
        self.assertGreater(len(fm), 200)
        self.assertEqual(text[text.index("## Safety"):], ref[ref.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("bar by bar", "can lag", "day trading loses most people money", "/nse-exit", "longs only",
                     "in the background", "/loop", "nothing was placed", "09:35 am", "₹1,015.00", "bar 3", "ist",
                     "nse/", "not financial advice", "yes", "diwali-balipratipada"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit", "trader" + "/", "new york", "nyse",
                      "eastern"):
            self.assertNotIn(never, low)
        self.assertNotRegex(text.replace("${CLAUDE_SKILL_DIR}", ""), r"\$\d")       # rupees, not dollars


if __name__ == "__main__":
    unittest.main()
