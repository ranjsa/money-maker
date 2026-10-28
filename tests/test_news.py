import json
import os
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("nse-news", "news")
DIR = os.path.join(FIX, "nse-news")
NOW = "2026-10-28 23:50"  # IST


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


class Names(unittest.TestCase):
    def test_normalise(self):
        n = news.normalise_name
        self.assertEqual(n("Reliance Industries Limited"), "reliance industries")
        self.assertEqual(n("RELIANCE INDUSTRIES LTD."), "reliance industries")
        self.assertEqual(n("Reliance  Industries Ltd"), "reliance industries")
        self.assertEqual(n("Mahindra & Mahindra Limited"), "mahindra and mahindra")
        self.assertEqual(n("Larsen & Toubro Ltd."), "larsen and toubro")
        self.assertEqual(n("UCO Bank"), "uco bank")
        self.assertEqual(n("Bajaj Auto Limited."), "bajaj auto")
        self.assertEqual(n("Limited Brands Ltd"), "limited brands")

    def test_google_url(self):
        url = news.google_url("Reliance Industries Limited")
        self.assertIn("q=%22Reliance%20Industries%22%20share", url)
        self.assertIn("hl=en-IN&gl=IN&ceid=IN:en", url)
        self.assertIn("q=%22Mahindra%20%26%20Mahindra%22%20share", news.google_url("Mahindra & Mahindra Limited"))
        self.assertIn("%22UCO%20Bank%22", news.google_url("UCO Bank"))


class Times(unittest.TestCase):
    def test_nse_pubdate_is_ist(self):
        t = news.parse_time("28-Oct-2026 21:01:36")
        self.assertEqual(t.isoformat(), "2026-10-28T21:01:36+05:30")

    def test_google_gmt_converted_to_ist(self):
        t = news.parse_time("Wed, 28 Oct 2026 01:49:56 GMT")
        self.assertEqual(t.isoformat(), "2026-10-28T07:19:56+05:30")
        self.assertIsNone(news.parse_time("not a date"))


class Tags(unittest.TestCase):
    def test_subject_map(self):
        t = news.tag_subject
        self.assertEqual(t("Analysts/Institutional Investor Meet/Con. Call Updates"), ["analyst"])
        self.assertEqual(t("Financial Result Updates"), ["earnings"])
        self.assertEqual(t("Outcome of Board Meeting"), ["board"])
        self.assertEqual(t("Board Meeting Intimation"), ["board"])
        self.assertEqual(t("Acquisition (including agreement to acquire)-XBRL"), ["deal"])
        self.assertEqual(t("Amalgamation OR Merger-XBRL"), ["deal"])
        self.assertEqual(t("Resignation of Director"), ["management"])
        self.assertEqual(t("Appointment of Director"), ["management"])
        self.assertEqual(t("Credit Rating"), ["rating"])
        self.assertEqual(t("Press Release"), ["filing"])
        self.assertEqual(t("outcome of board meeting - financial results"), ["earnings", "board"])

    def test_headline_keywords(self):
        k = news.headline_tags
        self.assertIn("earnings", k("Reliance Q2 results on October 30"))
        self.assertIn("earnings", k("TCS Q2 results: net profit rises"))
        self.assertIn("board", k("Board meeting on Friday to consider dividend"))
        self.assertIn("regulatory", k("SEBI bars promoter from market"))
        self.assertIn("macro", k("RBI keeps repo rate unchanged"))
        self.assertIn("analyst", k("Brokerage upgrades stock, target price Rs 1,600"))
        self.assertEqual(k("Sensex ends flat"), [])


class Feeds(unittest.TestCase):
    def test_announcements(self):
        items = news.parse_announcements(raw("Online_announcements.xml"))
        self.assertEqual(len(items), 30)
        uco = [i for i in items if i["company"] == "UCO Bank"][0]
        self.assertEqual(uco["subject"], "Analysts/Institutional Investor Meet/Con. Call Updates")
        self.assertEqual(uco["tags"], ["analyst"])
        self.assertEqual(uco["time"].isoformat(), "2026-10-28T21:01:36+05:30")
        self.assertTrue(uco["link"].endswith("NSEBSEROADSHOW.pdf"))
        self.assertEqual(uco["source"], "NSE announcement")

    def test_google(self):
        items = news.parse_google(raw("google-RELIANCE.xml"), "RELIANCE")
        self.assertEqual(len(items), 15)
        self.assertTrue(all(i["ticker"] == "RELIANCE" for i in items))
        mint = [i for i in items if i["source"] == "Livemint"][0]
        self.assertNotIn(" - Livemint", mint["title"])
        self.assertTrue(mint["link"].startswith("https://news.google.com/"))
        self.assertEqual(items[0]["time"].isoformat(), "2026-10-28T07:19:56+05:30")

    def test_duplicates_collapse(self):
        items = news.parse_google(raw("google-RELIANCE.xml"), "RELIANCE")
        slip = [i for i in items if i["title"].startswith("Reliance Industries shares slip 2%")]
        self.assertEqual(len(slip), 2)  # Livemint and TradingView carry the same headline
        out = news.dedupe(news.tag(items))
        self.assertEqual(len(out), 14)
        self.assertEqual(len([i for i in out if i["title"].startswith("Reliance Industries shares slip 2%")]), 1)

    def test_chart(self):
        m = news.parse_chart(raw("chart-RELIANCE.json"), "RELIANCE")
        self.assertEqual((m["price"], m["prev_close"]), (1158.0, 1196.2))
        self.assertEqual(m["pct"], -3.19)
        self.assertEqual(m["time"].isoformat(), "2026-10-28T15:28:22+05:30")
        with self.assertRaises(news.NewsError):
            news.parse_chart(b"{}", "X")


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.wl = os.path.join(t, "watchlist.json")
        with open(self.wl, "w") as fh:
            json.dump({"symbols": [{"symbol": "RELIANCE", "yahoo": "RELIANCE.NS", "setups": ["orb"]},
                                   {"symbol": "UCOBANK", "yahoo": "UCOBANK.NS", "setups": []}]}, fh)
        self.base = ["--offline", DIR, "--now", NOW, "--watchlist", self.wl,
                     "--universe", os.path.join(DIR, "universe.json"),
                     "--portfolio", os.path.join(t, "none.json"), "--plan", os.path.join(t, "none2.json"),
                     "--state", os.path.join(t, "state", "news.json")]

    def tearDown(self):
        self.tmp.cleanup()

    def test_watch_from_files(self):
        pf = os.path.join(self.tmp.name, "pf.json")
        with open(pf, "w") as fh:
            json.dump({"positions": [{"symbol": "POWERGRID", "qty": 10}]}, fh)
        w = news.watch_from_files(pf, self.wl, "/none.json", os.path.join(DIR, "universe.json"))
        self.assertEqual(w, {"POWERGRID": "Power Grid Corporation of India Limited",
                             "RELIANCE": "Reliance Industries Limited", "UCOBANK": "UCO Bank"})

    def test_announcement_matched_to_ticker(self):
        code, out, err = run(["--json", "--hours", "24"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        uco = [i for i in data["items"] if i["ticker"] == "UCOBANK"]
        self.assertEqual(len(uco), 1)
        self.assertEqual(uco[0]["tags"], ["analyst"])
        self.assertEqual(uco[0]["time"], "2026-10-28T21:01:36+05:30")
        self.assertEqual(uco[0]["source"], "NSE announcement")
        # nothing for companies we do not watch, and no fuzzy matches
        self.assertEqual({i["ticker"] for i in data["items"]}, {"RELIANCE", "UCOBANK"})
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        mv = data["moves"][0]
        self.assertEqual((mv["ticker"], mv["pct"], mv["moving"]), ("RELIANCE", -3.19, True))
        self.assertEqual(len([i for i in data["items"] if i["ticker"] == "RELIANCE"]), 14)

    def test_second_run_shows_nothing_new(self):
        code, _, err = run(["--hours", "24"] + self.base)
        self.assertEqual(code, 0, err)
        code, out, _ = run(["--hours", "24"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)
        code, out, _ = run(["--hours", "24", "--all"] + self.base)
        self.assertEqual(code, 1)  # exit 1 = nothing new, even when --all lists the old items
        self.assertIn("UCOBANK", out)

    def test_text_output_is_ist(self):
        code, out, err = run(["--hours", "24", "--tickers", "UCOBANK", "RELIANCE", "--dry-run"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("2026-10-28 21:01 IST", out)
        self.assertIn("MOVING", out)
        self.assertIn("-3.19%", out)
        self.assertIn("at 15:28 IST", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn(chr(0x2014), out)
        self.assertNotIn(" ET", out)

    def test_dry_run_leaves_state(self):
        run(["--hours", "24", "--dry-run"] + self.base)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "state", "news.json")))

    def test_window_hides_old_items(self):
        code, out, _ = run(["--json", "--hours", "1", "--dry-run"] + self.base)
        items = json.loads(out)["items"]
        self.assertEqual(code, 0)
        self.assertEqual({i["ticker"] for i in items}, {"RELIANCE"})  # UCO Bank filed at 21:01, 2h49m ago
        self.assertTrue(all(i["time"] >= "2026-10-28T22:50" for i in items))

    def test_no_tickers(self):
        args = list(self.base)
        args[args.index("--watchlist") + 1] = "/nonexistent.json"
        code, _, err = run(args)
        self.assertEqual(code, 2)
        self.assertIn("nse-scanner", err)


if __name__ == "__main__":
    unittest.main()
