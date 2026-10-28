import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

results = load("nse-analyst", "results")
DIR = os.path.join(FIX, "nse-analyst")
REV, NI, EPS = "quarterlyTotalRevenue", "quarterlyNetIncome", "quarterlyDilutedEPS"


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = results.main(args)
    return code, out.getvalue(), err.getvalue()


def fixture_series(sym):
    f = results.Fetcher(offline=DIR)
    return results.fetch_series(sym, fetcher=f)


class FiscalLabels(unittest.TestCase):
    def test_indian_fy(self):
        fl = results.fiscal_label
        self.assertEqual(fl("2026-06-30"), "Q1 FY27")
        self.assertEqual(fl("2025-09-30"), "Q2 FY26")
        self.assertEqual(fl("2025-12-31"), "Q3 FY26")
        self.assertEqual(fl("2025-03-31"), "Q4 FY25")


class Reliance(unittest.TestCase):
    def setUp(self):
        self.s = fixture_series("RELIANCE")
        self.r = results.analyse(self.s, quarters=6)

    def test_fetch_series_shape(self):
        self.assertEqual(self.s[NI][0], ("2025-03-31", 194070000000.0))
        self.assertEqual(len(self.s[REV]), 5)

    def test_newest_first(self):
        ends = [r["end"] for r in self.r["rows"]]
        self.assertEqual(ends, sorted(ends, reverse=True))
        self.assertEqual(ends[0], "2026-06-30")
        self.assertEqual(self.r["rows"][0]["fiscal"], "Q1 FY27")

    def test_q4_row_and_crore(self):
        q = next(r for r in self.r["rows"] if r["end"] == "2025-03-31")
        self.assertEqual(q["fiscal"], "Q4 FY25")
        self.assertEqual(q["net_income"], 194070000000.0)
        self.assertEqual(results.crore(q["net_income"]), "₹19,407.0 cr")
        self.assertEqual(q["eps_diluted"], 14.34)
        self.assertIn("₹19,407.0 cr", results.render("RELIANCE", "Reliance Industries Limited", self.r, None))

    def test_margins_and_yoy(self):
        q = self.r["rows"][0]
        self.assertEqual(q["gross_margin"], 26.8)
        self.assertEqual(q["operating_margin"], 10.5)
        self.assertEqual(q["net_margin"], 6.8)
        self.assertEqual(q["revenue_yoy"], 27.0)     # vs 2025-06-30
        self.assertEqual(q["net_income_yoy"], -22.4)
        self.assertEqual(q["eps_yoy"], -22.4)

    def test_yoy_missing_is_none_and_prints_na(self):
        q = next(r for r in self.r["rows"] if r["end"] == "2025-12-31")  # no 2024-12-31 in the data
        self.assertIsNone(q["revenue_yoy"])
        self.assertIsNone(q["net_income_yoy"])
        self.assertIsNone(q["eps_yoy"])
        text = results.render("RELIANCE", "Reliance Industries Limited", self.r, None)
        row = next(l for l in text.splitlines() if l.startswith("2025-12-31"))
        self.assertIn("n/a", row)
        self.assertNotIn("0.0%", row)

    def test_ttm_needs_four_consecutive_quarters(self):
        # 2025-09-30 is missing, so the last four are not consecutive
        self.assertIsNone(self.r["ttm"]["revenue"])
        self.assertIsNone(self.r["ttm"]["eps_diluted"])
        self.assertIn("n/a", results.render("RELIANCE", "Reliance Industries Limited", self.r, None)
                      .split("Last 4 quarters")[1].splitlines()[0])

    def test_ttm_when_complete(self):
        s = {REV: [("2025-09-30", 100.0), ("2025-12-31", 110.0), ("2026-03-31", 120.0), ("2026-06-30", 130.0)],
             NI: [("2025-09-30", 10.0), ("2025-12-31", 11.0), ("2026-03-31", 12.0), ("2026-06-30", 13.0)],
             EPS: [("2025-09-30", 1.0), ("2025-12-31", 1.1), ("2026-03-31", 1.2), ("2026-06-30", 1.3)]}
        t = results.analyse(s)["ttm"]
        self.assertEqual((t["revenue"], t["net_income"], t["eps_diluted"]), (460.0, 46.0, 4.6))

    def test_quarters_limit(self):
        self.assertEqual(len(results.analyse(self.s, quarters=2)["rows"]), 2)

    def test_changes_in_words(self):
        lines = results.changes(self.r)
        self.assertIn("Q1 FY27", lines[0])
        self.assertIn("revenue ₹3,09,468.0 cr (+27.0% vs a year earlier)", lines[0])
        self.assertIn("diluted EPS ₹15.48 (-22.4%)", lines[0])


class Bank(unittest.TestCase):
    def test_missing_gross_profit_margin_is_na(self):
        r = results.analyse(fixture_series("HDFCBANK"))
        q = r["rows"][0]
        self.assertEqual(q["end"], "2025-06-30")
        self.assertIsNone(q["gross_margin"])
        self.assertIsNone(q["operating_margin"])
        self.assertEqual(q["net_margin"], 19.0)
        self.assertEqual(q["revenue_yoy"], None)
        text = results.render("HDFCBANK", "HDFC Bank Limited", r, None)
        self.assertIn("n/a", next(l for l in text.splitlines() if l.startswith("2025-06-30")))

    def test_eps_missing_row(self):
        r = results.analyse(fixture_series("HDFCBANK"))
        self.assertIsNone(r["rows"][1]["eps_diluted"])


class Cleaning(unittest.TestCase):
    def test_only_3m_nulls_ignored_and_last_duplicate_wins(self):
        doc = {"timeseries": {"result": [{"meta": {"type": [REV]}, REV: [
            None,
            {"asOfDate": "2026-06-30", "periodType": "12M", "reportedValue": {"raw": 999.0}},
            {"asOfDate": "2026-06-30", "periodType": "3M", "reportedValue": {"raw": 1.0}},
            {"asOfDate": "2026-06-30", "periodType": "3M", "reportedValue": {"raw": 2.0}},
            {"asOfDate": "2026-03-31", "periodType": "3M", "reportedValue": None},
            {"asOfDate": "2026-03-31", "periodType": "3M", "reportedValue": {"raw": 5.0}},
        ]}]}}
        s = results.parse_series(doc)
        self.assertEqual(s[REV], [("2026-03-31", 5.0), ("2026-06-30", 2.0)])


class Filing(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(DIR, "Financial_Results.xml"), "rb") as fh:
            self.xml = fh.read()

    def test_matches_by_normalised_name_and_takes_newest(self):
        f = results.latest_filing(self.xml, "Reliance Industries Limited")
        self.assertEqual(f["period_ended"], "30-Jun-2026")
        self.assertEqual(f["relating_to"], "First Quarter")
        self.assertTrue(f["link"].endswith("INDAS_900002_1800002_17072026180000.xml"))
        self.assertEqual(f["published"], "17-Jul-2026 18:00:00")

    def test_no_partial_name_match(self):
        self.assertIsNone(results.latest_filing(self.xml, "Reliance"))
        self.assertIsNone(results.latest_filing(self.xml, None))

    def test_normalise(self):
        n = results.normalise_name
        self.assertEqual(n("Mahindra & Mahindra Limited"), n("MAHINDRA AND MAHINDRA LTD."))


class Cli(unittest.TestCase):
    def test_text_output(self):
        code, out, err = run(["RELIANCE", "--offline", DIR])
        self.assertEqual(code, 0, err)
        self.assertIn("Reliance Industries Limited (RELIANCE)", out)
        self.assertIn("Q1 FY27", out)
        self.assertIn("INDAS_900002_1800002_17072026180000.xml", out)
        self.assertIn("Yahoo", out)
        self.assertNotIn(chr(0x2014), out)

    def test_json_and_quarters(self):
        code, out, err = run(["RELIANCE", "--offline", DIR, "--json", "--quarters", "3"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["symbol"], "RELIANCE")
        self.assertEqual(len(data["result"]["rows"]), 3)
        self.assertEqual(data["filing"]["period_ended"], "30-Jun-2026")

    def test_no_data_is_plain_error(self):
        code, out, err = run(["ZZZZNOPE", "--offline", DIR])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)
        self.assertIn("no quarterly results", err)

    def test_empty_yahoo_answer_exits_1(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "timeseries-EMPTY.json"), "w") as fh:
                json.dump({"timeseries": {"result": [], "error": None}}, fh)
            code, out, err = run(["EMPTY", "--offline", tmp])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)

    def test_ampersand_symbol_url(self):
        self.assertIn("M%26M.NS", results.series_url("M&M", 1700000000))


if __name__ == "__main__":
    unittest.main()
