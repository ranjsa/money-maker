import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal

from _load import FIX, load

deals = load("nse-deals", "deals")
DIR = os.path.join(FIX, "nse-deals")
NOW = "2026-10-29 09:30"          # a Thursday, an open day
PIT_DIRECTOR = "IT_1197873_1662973_02052026044642_WEB.xml"
PIT_PLEDGE = "IT_1197871_1662943_02052026022032_WEB.xml"


def text(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = deals.main(args)
    return code, out.getvalue(), err.getvalue()


class Parse(unittest.TestCase):
    def test_bulk_row(self):
        rows = deals.parse_deals(text("bulk.csv"), "bulk")
        self.assertEqual(len(rows), 57)
        r = rows[0]
        self.assertEqual((r["date"], r["symbol"], r["client"], r["side"], r["qty"], r["kind"]),
                         ("2026-10-28", "AAATECH", "VAIBHAV RAJENDRA DOSHI", "BUY", 115017, "bulk"))
        self.assertEqual(r["name"], "AAA Technologies Limited")
        self.assertEqual(r["price"], Decimal("119.65"))
        self.assertEqual(r["value_inr"], Decimal("13761784.05"))
        self.assertIsInstance(r["qty"], int)

    def test_block_rows(self):
        rows = deals.parse_deals(text("block.csv"), "block")
        buy = [r for r in rows if r["side"] == "BUY"][0]
        self.assertEqual((buy["symbol"], buy["qty"], buy["price"], buy["kind"]),
                         ("STLTECH", 601900, Decimal("887.00"), "block"))
        self.assertEqual(buy["value_inr"], Decimal("533885300.00"))
        self.assertEqual({r["side"] for r in rows}, {"BUY", "SELL"})

    def test_junk_rows_skipped(self):
        csv_text = ("Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,"
                    "Trade Price / Wght. Avg. Price\n"
                    "28-OCT-2026,ABC,Abc Ltd,\"SMITH, JOHN\",sell,\"1,000\",10.5\n,,,,,,\nbad\n")
        rows = deals.parse_deals(csv_text, "block")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["client"], rows[0]["side"], rows[0]["qty"]), ("SMITH, JOHN", "SELL", 1000))

    def test_pit_director_buy(self):
        rows = deals.parse_pit_xbrl(text(PIT_DIRECTOR))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["symbol"], r["person"], r["category"], r["side"]),
                         ("RELTD", "Shantanu Lath", "Director", "BUY"))
        self.assertEqual(r["qty"], 70000)
        self.assertEqual(r["value_inr"], Decimal("7000000"))
        self.assertEqual((r["from_date"], r["to_date"], r["mode"]), ("2026-04-29", "2026-04-29", "ESOP"))
        self.assertEqual(r["company"], "Ravindra Energy Limited")

    def test_pit_promoter_pledge_is_not_a_buy(self):
        r = deals.parse_pit_xbrl(text(PIT_PLEDGE))[0]
        self.assertEqual(r["category"], "Promoter")
        self.assertEqual(r["side"], "PLEDGE")

    def test_pit_other_prefix_and_sale(self):
        xml = ('<x:xbrl xmlns:x="http://www.xbrl.org/2003/instance" xmlns:q="urn:other">'
               '<q:NSESymbol contextRef="OneI">ABC</q:NSESymbol>'
               '<q:NameOfTheCompany contextRef="OneI">Abc Ltd</q:NameOfTheCompany>'
               '<q:CategoryOfPerson contextRef="C1">Key Managerial Personnel</q:CategoryOfPerson>'
               '<q:NameOfThePerson contextRef="C1">A B</q:NameOfThePerson>'
               '<q:SecuritiesAcquiredOrDisposedNumberOfSecurity contextRef="C1">100</q:SecuritiesAcquiredOrDisposedNumberOfSecurity>'
               '<q:SecuritiesAcquiredOrDisposedValueOfSecurity contextRef="C1">25000.50</q:SecuritiesAcquiredOrDisposedValueOfSecurity>'
               '<q:SecuritiesAcquiredOrDisposedTransactionType contextRef="C1">Sell</q:SecuritiesAcquiredOrDisposedTransactionType>'
               '<q:ModeOfAcquisitionOrDisposal contextRef="C1">Market Sale</q:ModeOfAcquisitionOrDisposal>'
               '<q:CategoryOfPerson contextRef="C2">Designated Person</q:CategoryOfPerson>'
               '<q:NameOfThePerson contextRef="C2">C D</q:NameOfThePerson>'
               '<q:SecuritiesAcquiredOrDisposedNumberOfSecurity contextRef="C2">5</q:SecuritiesAcquiredOrDisposedNumberOfSecurity>'
               '<q:ModeOfAcquisitionOrDisposal contextRef="C2">Market Purchase</q:ModeOfAcquisitionOrDisposal>'
               '</x:xbrl>')
        rows = deals.parse_pit_xbrl(xml)
        self.assertEqual([(r["symbol"], r["person"], r["category"], r["side"], r["qty"]) for r in rows],
                         [("ABC", "A B", "KMP", "SELL", 100), ("ABC", "C D", "Designated person", "BUY", 5)])
        self.assertEqual(rows[0]["value_inr"], Decimal("25000.50"))

    def test_rss_items(self):
        items = deals.parse_insider_rss(text("Insider_Trading.xml"))
        self.assertEqual(len(items), 20)
        self.assertEqual(items[0]["company"], "Ravindra Energy Limited")
        self.assertTrue(items[0]["link"].endswith(PIT_DIRECTOR))
        self.assertEqual(items[0]["filed"], "2026-05-02T16:46+05:30")


class NewSince(unittest.TestCase):
    def setUp(self):
        self.rows = deals.parse_deals(text("bulk.csv"), "bulk")
        self.today = deals.parse_date("2026-10-29")

    def test_each_row_once(self):
        state = {"seen": [], "last_check": None, "history": {}}
        first = deals.new_since(self.rows, state, self.today)
        self.assertEqual(len(first), 57)
        state["seen"] = [deals.fingerprint(r) for r in first]
        state["last_check"] = "x"
        self.assertEqual(deals.new_since(self.rows, state, self.today), [])

    def test_fingerprint_covers_all_fields(self):
        a = dict(self.rows[0])
        b = dict(a, qty=a["qty"] + 1)
        self.assertNotEqual(deals.fingerprint(a), deals.fingerprint(b))
        self.assertEqual(deals.fingerprint(a), deals.fingerprint(dict(a)))

    def test_first_run_ignores_old_rows(self):
        old = dict(self.rows[0], date="2026-08-01")
        state = {"seen": [], "last_check": None}
        self.assertEqual(deals.new_since([old], state, self.today), [])


    def test_duplicate_row_in_one_batch_once(self):
        state = {"seen": [], "last_check": None}
        rows = self.rows[:2] + [dict(self.rows[0])]
        self.assertEqual(len(deals.new_since(rows, state, self.today)), 2)


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "deals.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW,
                     "--watchlist", os.path.join(self.tmp.name, "w.json"),
                     "--portfolio", os.path.join(self.tmp.name, "p.json")]

    def today(self, *extra):
        return run(["today"] + list(extra) + self.base)

    def test_today_shows_each_row_once(self):
        code, out, _ = self.today()
        self.assertEqual(code, 0)
        self.assertIn("AAATECH", out)
        self.assertIn("VAIBHAV RAJENDRA DOSHI", out)
        self.assertIn("₹1.4 cr", out)            # 13761784.05
        self.assertIn("STLTECH", out)
        self.assertIn("₹53.4 cr", out)            # 533885300
        code, out, _ = self.today()
        self.assertIn("nothing new", out.lower())
        self.assertNotIn("AAATECH", out)

    def test_newest_value_first_within_day(self):
        _, out, _ = self.today()
        self.assertLess(out.index("STLTECH"), out.index("AAATECH"))

    def test_watch_flags_only_that_symbol(self):
        code, out, _ = self.today("--watch", "STLTECH")
        lines = [l for l in out.splitlines() if "STLTECH" in l]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all("★ watching" in l for l in lines))
        self.assertNotIn("AAATECH", out)
        # rows that were filtered out are still new for a later plain run
        _, out, _ = self.today()
        self.assertIn("AAATECH", out)
        self.assertNotIn("STLTECH", out)

    def test_watchlist_file_marks_rows(self):
        with open(os.path.join(self.tmp.name, "p.json"), "w") as fh:
            json.dump({"positions": [{"symbol": "AAATECH"}]}, fh)
        _, out, _ = self.today()
        marked = [l for l in out.splitlines() if "★ watching" in l]
        self.assertEqual(len(marked), 2)          # the BUY and the SELL leg
        self.assertTrue(all("AAATECH" in l for l in marked))

    def test_min_value_cr_filters(self):
        _, out, _ = self.today("--min-value-cr", "1")
        self.assertIn("AAATECH", out)
        self.assertNotIn("LAMOSAIC", out)         # sub-1 cr rows
        self.assertIn("(51 of 59 fetched", out)
        _, out2, _ = self.today()
        self.assertIn("LAMOSAIC", out2)

    def test_closed_day(self):
        code, out, _ = run(["today"] + self.base[:-6] + ["--now", "2026-10-31 10:00"] + self.base[-4:])
        self.assertIn("no deals: market closed (weekend)", out)
        code, out, _ = run(["today", "--offline", DIR, "--state", self.state, "--now", "2026-11-10 10:00"])
        self.assertIn("no deals: market closed (Diwali-Balipratipada)", out)
        self.assertFalse(os.path.exists(self.state))

    def test_json_output(self):
        _, out, _ = self.today("--json")
        doc = json.loads(out)
        self.assertEqual(len(doc["new"]), 59)
        self.assertIn("value_inr", doc["new"][0])

    def test_client_searches_history(self):
        code, out, _ = run(["client", "doshi", "--state", self.state])
        self.assertEqual(code, 1)
        self.assertIn("No deals", out)
        self.today()
        code, out, _ = run(["client", "vaibhav rajendra", "--state", self.state])
        self.assertEqual(code, 0)
        self.assertIn("AAATECH", out)
        self.assertIn("115,017", out)
        self.assertNotIn("STLTECH", out)
        code, out, _ = run(["client", "serum institute", "--state", self.state])
        self.assertIn("SELL", out)
        self.assertIn("STLTECH", out)

    def test_insider(self):
        code, out, err = run(["insider"] + self.base)
        self.assertEqual(code, 0)
        self.assertIn("looks stale (latest 2026-05-02)", out)
        self.assertIn("nseindia.com > Corporate Filings > Insider Trading", out)
        self.assertIn("Shantanu Lath", out)
        self.assertIn("Director", out)
        self.assertIn("BUY", out)
        self.assertIn("₹0.7 cr", out)
        self.assertIn("Promoter", out)
        self.assertIn("PLEDGE", out)
        code, out, _ = run(["insider"] + self.base)
        self.assertIn("nothing new", out.lower())
        self.assertNotIn("Shantanu Lath", out)

    def test_repeated_rss_links_fetched_once(self):
        calls = []
        orig = deals.Fetcher.get

        def spy(self, url, name):
            calls.append(name)
            return orig(self, url, name)
        deals.Fetcher.get = spy
        try:
            with tempfile.TemporaryDirectory() as d:
                feed = text("Insider_Trading.xml")
                import re
                item = re.search(r"<item>.*?</item>", feed, re.S).group(0)
                with open(os.path.join(d, "Insider_Trading.xml"), "w", encoding="utf-8") as fh:
                    fh.write(feed.replace(item, item * 3, 1))
                for n in os.listdir(DIR):
                    if n.startswith("IT_"):
                        with open(os.path.join(d, n), "w", encoding="utf-8") as fh:
                            fh.write(text(n))
                _, out, _ = run(["insider", "--offline", d, "--state", self.state, "--now", NOW])
        finally:
            deals.Fetcher.get = orig
        self.assertEqual(calls.count(PIT_DIRECTOR), 1)
        self.assertEqual(out.count("Shantanu Lath"), 1)

    def test_insider_json_stale_goes_to_stderr(self):
        _, out, err = run(["insider", "--json"] + self.base)
        json.loads(out)
        self.assertIn("looks stale", err)

    def test_insider_not_stale_when_recent(self):
        _, out, _ = run(["insider"] + self.base[:-6] + ["--now", "2026-05-04 10:00"] + self.base[-4:])
        self.assertNotIn("stale", out)

    def test_insider_watch(self):
        _, out, _ = run(["insider", "--watch", "RELTD"] + self.base)
        self.assertIn("Shantanu Lath", out)
        self.assertIn("★ watching", out)
        self.assertNotIn("PLEDGE", out)
        # history is searchable by person
        _, out, _ = run(["client", "lath", "--state", self.state])
        self.assertIn("Shantanu Lath", out)


if __name__ == "__main__":
    unittest.main()
