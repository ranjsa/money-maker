"""The shared NSE block: IST clock, calendar, tick sizes, INR formatting. Loaded via nse-exit/exit.py."""

import datetime
import os
import subprocess
import sys
import unittest
from datetime import date, datetime as dt, timezone
from decimal import Decimal

from _load import ROOT, load

m = load("nse-exit", "exit")
utc = timezone.utc


class Clock(unittest.TestCase):
    def test_to_ist(self):
        self.assertEqual(m.to_ist(dt(2026, 10, 27, 3, 45, tzinfo=utc)).strftime("%H:%M"), "09:15")
        self.assertEqual(m.to_ist(dt(2026, 10, 27, 3, 45)).strftime("%H:%M"), "09:15")  # naive = UTC

    def test_ist_to_utc_and_parse_now(self):
        self.assertEqual(m.ist_to_utc(dt(2026, 10, 27, 9, 15)), dt(2026, 10, 27, 3, 45, tzinfo=utc))
        self.assertEqual(m.parse_now("2026-10-27 09:15"), dt(2026, 10, 27, 3, 45, tzinfo=utc))
        self.assertEqual(m.parse_now("2026-10-27 09:15 IST"), dt(2026, 10, 27, 3, 45, tzinfo=utc))
        self.assertEqual(m.parse_now("2026-10-27T03:45Z"), dt(2026, 10, 27, 3, 45, tzinfo=utc))
        self.assertIsNotNone(m.parse_now("").tzinfo)
        with self.assertRaises(ValueError):
            m.parse_now("tomorrow")


class Calendar(unittest.TestCase):
    def test_trading_day(self):
        self.assertEqual(m.trading_day(date(2026, 11, 10)), (False, "Diwali-Balipratipada"))
        self.assertEqual(m.trading_day(date(2026, 10, 24)), (False, "weekend"))
        self.assertEqual(m.trading_day(date(2027, 1, 4)), (True, "calendar not loaded"))
        self.assertEqual(m.trading_day(date(2026, 10, 27)), (True, ""))
        cfg = {"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}
        self.assertEqual(m.trading_day(date(2026, 11, 8), cfg), (True, "special session"))
        self.assertEqual(m.trading_day(date(2026, 11, 8)), (False, "weekend"))

    def test_holiday_table(self):
        self.assertEqual(len(m.NSE_HOLIDAYS), 16)
        self.assertEqual(m.CALENDAR_YEARS, (2026,))
        for iso in m.NSE_HOLIDAYS:
            self.assertLess(date.fromisoformat(iso).weekday(), 5, iso)

    def test_session(self):
        self.assertEqual(m.session(date(2026, 10, 27)), (datetime.time(9, 15), datetime.time(15, 30)))
        self.assertIsNone(m.session(date(2026, 11, 10)))
        cfg = {"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}
        self.assertEqual(m.session(date(2026, 11, 8), cfg), (datetime.time(18, 0), datetime.time(19, 0)))
        self.assertEqual((m.PREOPEN, m.OPEN, m.CLOSE),
                         (datetime.time(9, 0), datetime.time(9, 15), datetime.time(15, 30)))

    def test_market_status(self):
        s = lambda t, cfg=None: m.market_status(m.parse_now(t), cfg)
        self.assertEqual(s("2026-10-27 09:05")["state"], "pre-open")
        self.assertEqual(s("2026-10-27 09:15")["state"], "open")
        self.assertEqual(s("2026-10-27 15:29")["state"], "open")
        self.assertEqual(s("2026-10-27 15:30")["state"], "closed")
        self.assertEqual(s("2026-10-27 08:00")["next_open"], "2026-10-27T09:15+05:30")
        self.assertEqual(s("2026-10-27 15:31")["next_open"], "2026-10-28T09:15+05:30")
        self.assertEqual(s("2026-11-09 16:00")["next_open"], "2026-11-11T09:15+05:30")
        h = s("2026-11-10 10:00")
        self.assertEqual((h["state"], h["reason"]), ("closed", "Diwali-Balipratipada"))
        self.assertEqual(s("2026-10-24 10:00")["reason"], "weekend")
        cfg = {"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}
        self.assertEqual(s("2026-11-06 20:00", cfg)["next_open"], "2026-11-08T18:00+05:30")
        self.assertEqual(s("2026-11-08 18:30", cfg)["state"], "open")


class Tick(unittest.TestCase):
    def test_slabs(self):
        for p, t in [(249.99, "0.01"), (250, "0.05"), (1000, "0.05"), (1000.05, "0.10"), (5000, "0.10"),
                     (5000.5, "0.50"), (10000, "0.50"), (15000, "1.00"), (20000, "1.00"), (25000, "5.00")]:
            self.assertEqual(m.tick_for(p), Decimal(t), p)
        self.assertEqual(m.tick_for(Decimal("1000.05")), Decimal("0.10"))

    def test_rounding(self):
        ref = Decimal("1015")
        self.assertEqual(m.floor_tick(Decimal("1005.25"), ref), Decimal("1005.20"))
        self.assertEqual(m.round_tick(Decimal("1005.25"), ref), Decimal("1005.30"))
        self.assertEqual(m.round_tick(Decimal("1005.24"), ref), Decimal("1005.20"))
        self.assertEqual(m.ceil_tick(Decimal("1005.21"), ref), Decimal("1005.30"))
        self.assertEqual(m.floor_tick(280.47, 280), Decimal("280.45"))
        self.assertEqual(m.floor_tick(Decimal("7.126"), Decimal("7")), Decimal("7.12"))


class Money(unittest.TestCase):
    def test_inr(self):
        self.assertEqual(m.inr(12345678.9), "₹1,23,45,678.90")
        self.assertEqual(m.inr(0), "₹0.00")
        self.assertEqual(m.inr(999), "₹999.00")
        self.assertEqual(m.inr(1000), "₹1,000.00")
        self.assertEqual(m.inr(-123456.5), "-₹1,23,456.50")
        self.assertEqual(m.inr(1234.5, 0), "₹1,235")
        self.assertEqual(m.crore(1.9407e11), "₹19,407.0 cr")

    def test_symbols(self):
        self.assertEqual(m.yahoo_quote("M&M"), "M%26M.NS")
        self.assertEqual(m.yahoo_symbol("M&M"), "M&M.NS")
        self.assertEqual(m.yahoo_symbol("BAJAJ-AUTO"), "BAJAJ-AUTO.NS")
        self.assertEqual(m.yahoo_symbol(" reliance "), "RELIANCE.NS")
        self.assertEqual(m.yahoo_symbol("RELIANCE.NS"), "RELIANCE.NS")
        self.assertEqual(m.yahoo_quote("J&KBANK"), "J%26KBANK.NS")


class EveryTool(unittest.TestCase):
    def test_all_tools_carry_the_block_and_import(self):
        for folder in sorted(d for d in os.listdir(ROOT) if d.startswith("nse-")):
            for f in os.listdir(os.path.join(ROOT, folder)):
                if f.endswith(".py"):
                    mod = load(folder, f[:-3])
                    self.assertTrue(callable(mod.to_ist), folder)
                    self.assertTrue(callable(mod.tick_for), folder)

    def test_help_runs(self):
        for folder in sorted(d for d in os.listdir(ROOT) if d.startswith("nse-")):
            for f in os.listdir(os.path.join(ROOT, folder)):
                if f.endswith(".py"):
                    r = subprocess.run([sys.executable, os.path.join(ROOT, folder, f), "--help"],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self.assertEqual(r.returncode, 0, "%s/%s: %s" % (folder, f, r.stderr.decode()[-300:]))


if __name__ == "__main__":
    unittest.main()
