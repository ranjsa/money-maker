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

from _load import load, ROOT, FIX

alerts = load("nse-alerts", "alerts")

QDIR = os.path.join(FIX, "nse-alerts")
PLAN = os.path.join(FIX, "nse-budget", "plan-demo.json")
DT = datetime.datetime
UA = "Mozilla/5.0 (nse-day-trader-skill)"

# NSE CM trading holidays, weekdays only (holiday-master?type=trading, key CM, fetched 2026-10-28)
NSE_2026 = ["2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14",
            "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10",
            "2026-11-24", "2026-12-25"]

# the saved quotes (fetched 2026-10-28): price, previous close, quote time
REL = (1158.0, 1196.2, "2026-10-28T09:58:22Z")


def ist(text):
    return alerts.parse_now(text)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_rules():
    with open(PLAN) as fh:
        return alerts.rules_from_plan(json.load(fh))


def rel_plan(day="2026-10-28"):
    return {"created_by": "nse-exit", "symbol": "RELIANCE", "side": "buy", "date": day, "entry": 1180.0,
            "stop": 1150.0, "stop_label": "opening-range midpoint", "target": 1200.0,
            "target_label": "1R (one times the risk)", "flat_by": "15:10"}


class Calendar(unittest.TestCase):
    def test_holidays_match_the_feed(self):
        self.assertEqual(sorted(alerts.NSE_HOLIDAYS), NSE_2026)
        for d in alerts.NSE_HOLIDAYS:
            self.assertLess(datetime.date.fromisoformat(d).weekday(), 5, d)
        self.assertEqual(alerts.CALENDAR_YEARS, (2026,))

    def test_parse_now_is_ist(self):
        self.assertEqual(ist("2026-10-28 09:15"), DT(2026, 10, 28, 3, 45, tzinfo=datetime.timezone.utc))
        self.assertEqual(ist("2026-10-28T10:15 IST"), DT(2026, 10, 28, 4, 45, tzinfo=datetime.timezone.utc))
        self.assertEqual(ist("2026-10-28T04:45:01Z"), DT(2026, 10, 28, 4, 45, 1, tzinfo=datetime.timezone.utc))
        with self.assertRaises(ValueError):
            ist("tomorrow")

    def test_market_status(self):
        self.assertEqual(alerts.market_status(ist("2026-10-28 10:00"))["state"], "open")
        self.assertEqual(alerts.market_status(ist("2026-10-28 09:05"))["state"], "pre-open")
        self.assertEqual(alerts.market_status(ist("2026-10-28 09:14"))["state"], "pre-open")
        self.assertEqual(alerts.market_status(ist("2026-10-28 09:15"))["state"], "open")
        self.assertEqual(alerts.market_status(ist("2026-10-28 15:30"))["state"], "closed")
        after = alerts.market_status(ist("2026-10-28 15:30"))
        self.assertEqual(after["next_open"], "2026-10-29T09:15+05:30")
        dussehra = alerts.market_status(ist("2026-10-20 11:00"))
        self.assertEqual((dussehra["state"], dussehra["reason"]), ("closed", "Dussehra"))
        self.assertEqual(dussehra["next_open"], "2026-10-21T09:15+05:30")
        self.assertEqual(alerts.market_status(ist("2026-10-31 11:00"))["next_open"], "2026-11-02T09:15+05:30")
        self.assertEqual(alerts.market_status(ist("2026-11-10 10:00"))["reason"], "Diwali-Balipratipada")

    def test_special_session_from_config(self):
        cfg = {"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}
        self.assertEqual(alerts.market_status(ist("2026-11-08 18:30"), cfg)["state"], "open")
        self.assertEqual(alerts.market_status(ist("2026-11-08 18:30"))["state"], "closed")

    def test_out_of_calendar_year_is_flagged(self):
        self.assertIn("NSE calendar for 2027 not loaded", alerts.calendar_warning(datetime.date(2027, 3, 1)))
        self.assertIn("alerts.py calendar --refresh", alerts.calendar_warning(datetime.date(2027, 3, 1)))
        self.assertEqual(alerts.calendar_warning(datetime.date(2026, 3, 2)), "")
        self.assertEqual(alerts.market_status(ist("2027-03-01 10:00"))["state"], "open")


class Quotes(unittest.TestCase):
    def test_yahoo_fixtures(self):
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json")) as fh:
            q = alerts.parse_yahoo(fh.read(), "RELIANCE")
        self.assertEqual((q["price"], q["prev_close"], q["time"]), REL)
        self.assertEqual((q["open"], q["currency"]), (1194.5, "INR"))
        with open(os.path.join(QDIR, "yahoo-M&M.json")) as fh:
            q = alerts.parse_yahoo(fh.read(), "M&M")
        self.assertEqual((q["symbol"], q["price"], q["prev_close"]), ("M&M", 2869.9, 2858.4))

    def test_previous_close_falls_back_to_previousClose(self):
        doc = {"chart": {"result": [{"meta": {"regularMarketPrice": 10.0, "previousClose": 9.5,
                                              "regularMarketTime": 1793181502}}], "error": None}}
        self.assertEqual(alerts.parse_yahoo(json.dumps(doc), "X")["prev_close"], 9.5)
        doc["chart"]["result"][0]["meta"]["chartPreviousClose"] = 9.0
        self.assertEqual(alerts.parse_yahoo(json.dumps(doc), "X")["prev_close"], 9.0)

    def test_urls_and_user_agent(self):
        self.assertEqual(alerts.UA, UA)
        self.assertEqual(alerts.yahoo_url("reliance"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1d")
        self.assertIn("/chart/M%26M.NS?", alerts.yahoo_url("M&M"))
        self.assertIn("/chart/BAJAJ-AUTO.NS?", alerts.yahoo_url("bajaj-auto"))
        self.assertIn("//query1.finance.yahoo.com/", alerts.yahoo_url("TCS", alerts.YAHOO_HOSTS[1]))
        self.assertFalse(hasattr(alerts, "stooq_url"))

    def test_offline_quotes_and_errors(self):
        q, errors = alerts.get_quotes(["RELIANCE", "TCS", "ZZZZNOPE", "NOFILE"], QDIR)
        self.assertEqual(sorted(q), ["RELIANCE", "TCS"])
        self.assertEqual(q["TCS"]["price"], 2460.0)
        self.assertEqual(len(errors), 2)
        self.assertIn("No data found", errors[0])

    def test_offline_gz_and_suffix(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "yahoo-RELIANCE.json"), "rb") as src:
                with gzip.open(os.path.join(d, "yahoo-RELIANCE.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.get_quotes(["reliance.ns"], d)
            self.assertEqual((q["RELIANCE"]["price"], errors), (1158.0, []))
        finally:
            shutil.rmtree(d)


class FakeNet(object):
    """Stands in for urllib: answers each request with the next status code."""

    def __init__(self, codes, body=None):
        self.codes, self.calls, self.sleeps = list(codes), [], []
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json")) as fh:
            self.body = body or fh.read()

    def urlopen(self, req, timeout=None):
        self.calls.append((req.full_url.split("/")[2], req.get_header("User-agent")))
        code = self.codes.pop(0)
        if code != 200:
            fp = io.BytesIO(b'{"chart":{"result":null,"error":{"code":"Not Found",'
                            b'"description":"No data found, symbol may be delisted"}}}' if code == 404 else b"")
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, fp)
        return io.BytesIO(self.body.encode("utf-8"))


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (alerts._urlopen, alerts._sleep)

    def tearDown(self):
        alerts._urlopen, alerts._sleep = self.saved

    def net(self, codes):
        f = FakeNet(codes)
        alerts._urlopen = f.urlopen
        alerts._sleep = f.sleeps.append
        return f

    def hosts(self, f):
        return [h.split(".")[0] for h, _ in f.calls]

    def test_query2_first_with_the_contract_user_agent(self):
        f = self.net([200])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual((q["RELIANCE"]["price"], errors), (1158.0, []))
        self.assertEqual(f.calls, [("query2.finance.yahoo.com", UA)])

    def test_one_retry_with_a_pause_on_429(self):
        f = self.net([429, 200])
        self.assertEqual(alerts.get_quotes(["RELIANCE"])[0]["RELIANCE"]["price"], 1158.0)
        self.assertEqual(self.hosts(f), ["query2", "query2"])
        self.assertEqual(f.sleeps, [alerts.BACKOFF])

    def test_falls_back_to_query1_on_429_and_5xx(self):
        f = self.net([429, 429, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query2", "query1"])
        f = self.net([503, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query1"])
        f = self.net([500, 502])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual(q, {})
        self.assertIn("query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS answered HTTP 502", errors[0])

    def test_a_404_is_yahoos_answer_and_a_403_is_not_retried(self):
        f = self.net([404])
        q, errors = alerts.get_quotes(["NOPE"])
        self.assertIn("No data found", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])
        f = self.net([403])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertIn("HTTP 403", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])

    def test_a_pause_between_tickers(self):
        f = self.net([200, 200])
        alerts.get_quotes(["RELIANCE", "TCS"])
        self.assertEqual(f.sleeps, [alerts.PAUSE])


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, _ = alerts.get_quotes(["RELIANCE", "TCS", "M&M"], QDIR)
        self.now = ist("2026-10-28 10:30")
        self.open = alerts.market_status(self.now)

    def at(self, text, rules, state=None, quotes=None, broker=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, alerts.market_status(now),
                               state or {}, now, broker=broker)

    def test_a_target_rings_once_per_day(self):
        rules = [{"id": "rel-1150", "symbol": "RELIANCE", "type": "above", "price": 1150}]
        rings, st = self.at("2026-10-28 10:30", rules)
        self.assertEqual(len(rings), 1)
        self.assertIn("RELIANCE is ₹1,158.00: at or above your alert level ₹1,150.00", rings[0]["message"])
        rings, st = self.at("2026-10-28 10:35", rules, st)
        self.assertEqual(rings, [])

    def test_stop_and_rearm(self):
        rules = [{"id": "rel-stop", "symbol": "RELIANCE", "type": "stop", "price": 1160, "rearm": True}]
        rings, st = self.at("2026-10-28 10:30", rules)
        self.assertIn("your stop ₹1,160.00", rings[0]["message"])
        self.q["RELIANCE"]["price"] = 1165.0
        rings, st = self.at("2026-10-28 10:31", rules, st)
        self.assertEqual(rings, [])
        self.assertNotIn("rel-stop", st["rung"])
        self.q["RELIANCE"]["price"] = 1155.0
        self.assertEqual(len(self.at("2026-10-28 10:32", rules, st)[0]), 1)

    def test_pct_move_uses_the_previous_close_from_yahoo_meta(self):
        big = [{"id": "r-5", "symbol": "RELIANCE", "type": "pct_move", "pct": 5}]
        self.assertEqual(self.at("2026-10-28 10:30", big)[0], [])
        small = [{"id": "r-3", "symbol": "RELIANCE", "type": "pct_move", "pct": 3, "direction": "down"}]
        rings, st = self.at("2026-10-28 10:30", small)
        self.assertEqual(rings[0]["message"], "RELIANCE moved -3.19% to ₹1,158.00 (from ₹1,196.20).")
        self.assertEqual(self.at("2026-10-28 10:40", small, st)[0], [])          # once per day
        self.assertEqual(len(self.at("2026-10-29 10:40", small, st)[0]), 1)      # again the next day
        up = [{"id": "r-up", "symbol": "RELIANCE", "type": "pct_move", "pct": 3, "direction": "up"}]
        self.assertEqual(self.at("2026-10-28 10:30", up)[0], [])
        mm = [{"id": "mm", "symbol": "M&M", "type": "pct_move", "pct": 0.3}]
        self.assertIn("M&M moved +0.40% to ₹2,869.90 (from ₹2,858.40)", self.at("2026-10-28 10:30", mm)[0][0]["message"])
        ref = [{"id": "ref", "symbol": "RELIANCE", "type": "pct_move", "pct": 1, "ref": 1100}]
        self.assertIn("(from ₹1,100.00)", self.at("2026-10-28 10:30", ref)[0][0]["message"])

    def test_opening_bell_rings_at_0915_on_trading_days_only(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        self.assertEqual(self.at("2026-10-28 09:10", rules)[0], [])               # pre-open is not the bell
        self.assertEqual(self.at("2026-10-28 09:14", rules)[0], [])
        rings, st = self.at("2026-10-28 09:15", rules)
        self.assertEqual(len(rings), 1)
        self.assertIn("Opening bell", rings[0]["message"])
        self.assertIn("09:15 IST", rings[0]["message"])
        self.assertEqual(self.at("2026-10-28 11:00", rules, st)[0], [])           # once
        self.assertEqual(len(self.at("2026-10-29 09:16", rules, st)[0]), 1)       # next trading day
        self.assertEqual(self.at("2026-10-31 09:20", rules)[0], [])               # Saturday
        self.assertEqual(self.at("2026-11-01 09:20", rules)[0], [])               # Sunday
        self.assertEqual(self.at("2026-10-20 09:20", rules)[0], [])               # Dussehra
        self.assertEqual(self.at("2026-11-10 09:20", rules)[0], [])               # Diwali-Balipratipada
        self.assertEqual(self.at("2026-10-28 15:45", rules)[0], [])               # after the close

    def test_opening_bell_on_a_special_session_rings_at_its_start(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        cfg = {"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}
        now = ist("2026-11-08 18:01")
        rings, _ = alerts.evaluate(rules, {}, alerts.market_status(now, cfg), {}, now, cfg=cfg)
        self.assertEqual(len(rings), 1)
        now = ist("2026-11-08 09:20")
        self.assertEqual(alerts.evaluate(rules, {}, alerts.market_status(now, cfg), {}, now, cfg=cfg)[0], [])

    def test_the_plan_gives_a_stop_a_target_and_a_flat_by_time(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("demo-plan-stop", "stop"), ("demo-plan-target", "target"), ("demo-flat-by", "time")])
        self.assertEqual((rules[0]["price"], rules[1]["price"]), (1005.2, 1034.6))
        self.assertEqual((rules[2]["at"], rules[2]["label"]), ("15:10", "flat by"))
        self.assertTrue(all(r["date"] == "2026-10-27" for r in rules))
        self.assertNotIn("before_close", rules[2])
        self.assertEqual(alerts.rules_from_plan({"symbol": "X", "stop": 9.0, "target": 11.0})[2]["at"], "15:10")
        self.assertEqual(alerts.rules_from_plan({"symbol": "X", "stop": 9.0, "target": 11.0}, "14:50")[2]["at"], "14:50")
        self.assertEqual(alerts.rules_from_plan({"symbol": "X", "stop": 9.0, "target": 11.0, "flat_by": "15:00"},
                                                "14:50")[2]["at"], "15:00")

    def test_plan_stop_and_target_ring(self):
        rules = alerts.rules_from_plan(rel_plan())
        rings, st = self.at("2026-10-28 10:30", rules)                       # 1158 sits between 1150 and 1200
        self.assertEqual(rings, [])
        q = json.loads(json.dumps(self.q))
        q["RELIANCE"]["price"] = 1149.5
        rings, st = self.at("2026-10-28 11:30", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-stop"])
        self.assertIn("RELIANCE is ₹1,149.50: at or below your stop ₹1,150.00 (plan stop: opening-range midpoint)",
                      rings[0]["message"])
        q["RELIANCE"]["price"] = 1200.0
        self.assertEqual([r["id"] for r in self.at("2026-10-28 12:00", rules, st, q)[0]], ["reliance-plan-target"])
        self.assertEqual(self.at("2026-10-29 12:00", rules, {}, q)[0], [])    # plan rules: their day only

    def test_flat_by_rings_once_per_trading_day(self):
        rules = alerts.rules_from_plan(rel_plan())[2:]
        self.assertEqual(self.at("2026-10-28 15:09", rules)[0], [])
        rings, st = self.at("2026-10-28 15:10", rules)
        self.assertEqual(len(rings), 1)
        self.assertIn("Flat by: it is 15:10 IST (set for 15:10), the market closes at 15:30", rings[0]["message"])
        self.assertIn("nse-broker/ticket.py flatten --symbol RELIANCE", rings[0]["message"])
        self.assertEqual(self.at("2026-10-28 15:15", rules, st)[0], [])
        rings, _ = self.at("2026-10-28 15:40", rules)
        self.assertIn("the market has closed", rings[0]["message"])

    def test_time_rule_skips_weekends_and_holidays(self):
        rule = [{"id": "t", "type": "time", "at": "15:10", "label": "check"}]
        self.assertEqual(self.at("2026-10-31 15:11", rule)[0], [])          # Saturday
        self.assertEqual(self.at("2026-10-20 15:11", rule)[0], [])          # Dussehra
        rings, st = self.at("2026-10-27 15:11", rule)
        self.assertEqual(len(rings), 1)
        self.assertEqual(st["rung"]["t"], "2026-10-27T09:41:00Z")           # stamped in UTC
        self.assertEqual(len(self.at("2026-10-28 15:11", rule, st)[0]), 1)  # a new day rings again

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "nse-alerts", "alerts.example.json")) as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        ids = [r["id"] for r in rules]
        self.assertIn("opening-bell", ids)
        plan = [r for r in rules if "-plan-" in r["id"] or r["id"].endswith("-flat-by")]
        self.assertEqual(plan, [dict(r, date="2026-10-28") for r in alerts.rules_from_plan(
            dict(rel_plan(), stop=1150.0, target=1200.0))])

    def test_bad_rules(self):
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "moon"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "above", "symbol": "TCS"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "time", "at": "4pm"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "RELIANCE", "stop": 1150.0})


def broker_doc(records):
    return {"orders": records}


def rec(leg, status, group="RELIANCE-1", day="2026-10-28", fp="abc123", sym="RELIANCE", qty=10, price=None,
        at="2026-10-28T10:00:00+05:30"):
    r = {"at": at, "date": day, "group": group, "fingerprint": fp, "leg": leg, "symbol": sym, "qty": qty,
         "status": status}
    if price is not None:
        r["price"] = price
    return r


class OrphanLeg(unittest.TestCase):
    BASE = [rec("entry", "approved"), rec("entry", "filled", price=1180.0), rec("stop", "placed"),
            rec("target", "placed")]

    def rings(self, records, text="2026-10-28 11:00", state=None):
        now = ist(text)
        return alerts.evaluate([], {}, alerts.market_status(now), state or {}, now, broker=broker_doc(records))

    def test_both_legs_open_after_the_entry_is_quiet(self):
        self.assertEqual(self.rings(self.BASE)[0], [])

    def test_a_filled_target_with_the_stop_still_open_rings_once(self):
        records = self.BASE + [rec("target", "filled", price=1200.0)]
        rings, st = self.rings(records)
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0]["message"], "RELIANCE: target filled, cancel the stop leg now "
                         "(nse-broker/ticket.py fill printed the cancel_order ticket)")
        self.assertEqual(rings[0]["symbol"], "RELIANCE")
        self.assertEqual(self.rings(records, "2026-10-28 11:05", st)[0], [])      # once per day

    def test_a_filled_stop_with_the_target_still_open_rings(self):
        rings, _ = self.rings(self.BASE + [rec("stop", "filled", price=1150.0)])
        self.assertIn("RELIANCE: stop filled, cancel the target leg now", rings[0]["message"])

    def test_a_cancelled_or_filled_sibling_does_not_ring(self):
        done = self.BASE + [rec("target", "filled", price=1200.0), rec("stop", "cancelled")]
        self.assertEqual(self.rings(done)[0], [])
        both = self.BASE + [rec("target", "filled", price=1200.0), rec("stop", "filled", price=1150.0)]
        self.assertEqual(self.rings(both)[0], [])

    def test_a_sibling_that_was_never_placed_does_not_ring(self):
        records = [rec("entry", "filled", price=1180.0), rec("stop", "placed"), rec("target", "filled", price=1200.0),
                   rec("stop", "cancelled")]
        self.assertEqual(self.rings(records)[0], [])
        records = [rec("entry", "filled", price=1180.0), rec("target", "filled", price=1200.0)]
        self.assertEqual(self.rings(records)[0], [])

    def test_a_flatten_fill_leaves_both_legs_to_cancel(self):
        rings, _ = self.rings(self.BASE + [rec("exit", "filled", price=1170.0)])
        self.assertEqual(sorted(r["message"].split(" now")[0] for r in rings),
                         ["RELIANCE: exit filled, cancel the stop leg", "RELIANCE: exit filled, cancel the target leg"])

    def test_only_todays_fills_and_the_same_group_count(self):
        old = [dict(r, date="2026-10-27") for r in self.BASE + [rec("target", "filled", price=1200.0)]]
        self.assertEqual(self.rings(old)[0], [])
        other = self.BASE + [rec("target", "filled", price=1200.0, group="TCS-1", fp="zzz", sym="TCS")]
        self.assertEqual(self.rings(other)[0], [])               # TCS-1 has no placed stop; RELIANCE-1 has no exit fill

    def test_two_groups_ring_separately(self):
        a = [rec("entry", "filled", price=1180.0), rec("stop", "placed"), rec("target", "placed"),
             rec("target", "filled", price=1200.0)]
        b = [rec("entry", "filled", group="TCS-1", fp="zzz", sym="TCS"), rec("stop", "placed", group="TCS-1", fp="zzz", sym="TCS"),
             rec("target", "placed", group="TCS-1", fp="zzz", sym="TCS"),
             rec("stop", "filled", group="TCS-1", fp="zzz", sym="TCS")]
        rings, _ = self.rings(a + b)
        self.assertEqual(sorted(r["symbol"] for r in rings), ["RELIANCE", "TCS"])

    def test_check_rings_orphans_with_no_rules_file(self):
        d = tempfile.mkdtemp()
        try:
            broker = os.path.join(d, "broker.json")
            with open(broker, "w") as fh:
                json.dump(broker_doc(self.BASE + [rec("target", "filled", price=1200.0)]), fh)
            args = ["check", "--rules", os.path.join(d, "none.json"), "--state", os.path.join(d, "alerts-state.json"),
                    "--prices-out", os.path.join(d, "prices.json"), "--broker", broker, "--offline", QDIR,
                    "--now", "2026-10-28 11:00"]
            code, text, _ = run(args)
            self.assertEqual(code, 0)
            self.assertIn("ALERT RELIANCE: target filled, cancel the stop leg now", text)
            code, text, _ = run(args)
            self.assertEqual(code, 1)
            self.assertNotIn("ALERT", text)
            code, _, err = run(args[:7] + ["--broker", os.path.join(d, "missing.json")] + args[9:])
            self.assertEqual(code, 1)
            self.assertIn("no rules", err)
        finally:
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")
        self.plan = os.path.join(self.d, "plan.json")
        self.broker = os.path.join(self.d, "none-broker.json")
        with open(self.plan, "w") as fh:
            json.dump(rel_plan(), fh)

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--broker", self.broker, "--offline", QDIR, "--now", now])

    def test_the_trade_from_the_plan(self):
        code, text, _ = run(["add", "--from-plan", "--plan", self.plan, "--rules", self.rules, "--state", self.state,
                             "--config", os.path.join(self.d, "none")])
        self.assertEqual(code, 0)
        self.assertIn("Added reliance-flat-by", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--opening-bell"])[0], 0)

        code, text, _ = self.check("2026-10-28 09:20")
        self.assertEqual(code, 0)                                      # the opening bell rings first
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-28 09:21")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit. 4 rule(s) watched today, RELIANCE ₹1,158.00.", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"RELIANCE": 1158.0})
        self.assertEqual(doc["detail"]["RELIANCE"]["time"], "2026-10-28T09:58:22Z")
        self.assertEqual(doc["detail"]["RELIANCE"]["fetched"], "2026-10-28T03:51:00Z")

        code, text, _ = self.check("2026-10-28 15:10")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by", text)
        self.assertIn("not an order", text)
        self.assertEqual(self.check("2026-10-28 15:12")[0], 1)
        code, text, _ = self.check("2026-10-29 09:16")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-29 09:25")
        self.assertIn("Nothing hit. 1 rule(s) watched today.", text)

        # a new plan replaces the plan rules and re-arms them
        run(["add", "--from-plan", "--plan", self.plan, "--rules", self.rules, "--state", self.state])
        with open(self.rules) as fh:
            ids = [r["id"] for r in json.load(fh)["rules"]]
        self.assertEqual(sorted(ids), ["opening-bell", "reliance-flat-by", "reliance-plan-stop", "reliance-plan-target"])
        with open(self.state) as fh:
            self.assertNotIn("reliance-flat-by", json.load(fh)["rung"])

    def test_check_on_a_holiday_and_out_of_calendar(self):
        run(["add", "--rules", self.rules, "--opening-bell"])
        code, text, _ = self.check("2026-10-20 09:30")
        self.assertEqual(code, 1)
        self.assertIn("NSE closed (2026-10-20 09:30 IST, Dussehra)", text)
        code, text, _ = self.check("2027-03-01 09:30")
        self.assertIn("NSE calendar for 2027 not loaded: run nse-alerts/alerts.py calendar --refresh "
                      "or check nseindia.com", text)

    def test_add_time_and_target(self):
        code, text, _ = run(["add", "--rules", self.rules, "--at", "15:10", "--label", "flat by",
                             "--symbol", "reliance"])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "RELIANCE", "--target", "1200.5",
                             "--date", "2026-10-28"])
        self.assertEqual(code, 0)
        with open(self.rules) as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[0], {"id": "time-1510", "type": "time", "at": "15:10", "label": "flat by",
                                    "symbol": "RELIANCE"})
        self.assertEqual(rules[1], {"id": "reliance-target-1200.50", "symbol": "RELIANCE", "type": "target",
                                    "price": 1200.5, "date": "2026-10-28"})
        self.assertEqual(run(["add", "--rules", self.rules, "--at", "4pm"])[0], 1)
        code, _, err = run(["add", "--rules", self.rules, "--from-plan", "--plan", os.path.join(self.d, "none")])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "RELIANCE", "--target", "1200.5",
                            "--date", "2026-10-28"])
        self.assertIn("already exists", err)

    def test_quote_write_shape_for_nse_risk(self):
        code, text, _ = run(["quote", "RELIANCE", "M&M", "--offline", QDIR, "--write", self.prices,
                             "--now", "2026-10-28T10:00:00+05:30"])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE", text)
        self.assertIn("₹1,158.00 (-3.19% vs prev close ₹1,196.20)", text)
        self.assertIn("delayed", text)
        self.assertIn("Wrote 2 price(s)", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc), ["as_of", "currency", "detail", "note", "prices", "source"])
        self.assertEqual((doc["currency"], doc["source"], doc["as_of"]), ("INR", "yahoo", "2026-10-28T04:30:00Z"))
        self.assertEqual(doc["prices"], {"RELIANCE": 1158.0, "M&M": 2869.9})
        self.assertEqual(doc["detail"]["RELIANCE"], {"prev_close": 1196.2, "time": "2026-10-28T09:58:22Z",
                                                     "source": "yahoo", "fetched": "2026-10-28T04:30:00Z"})
        self.assertEqual(doc["detail"]["M&M"]["prev_close"], 2858.4)
        run(["quote", "TCS", "--offline", QDIR, "--write", self.prices])
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["prices"]), ["M&M", "RELIANCE", "TCS"])

    def test_the_written_prices_satisfy_nse_risk_load_prices(self):
        risk = load("nse-risk", "check")
        run(["quote", "RELIANCE", "--offline", QDIR, "--write", self.prices, "--now", "2026-10-28T10:00:00+05:30"])
        with open(self.prices) as fh:
            doc = json.load(fh)
        prices, times, prev = risk.load_prices(doc, "2026-10-28")
        self.assertEqual((prices["RELIANCE"], prev["RELIANCE"]), (1158.0, 1196.2))
        self.assertEqual(times["RELIANCE"], DT(2026, 10, 28, 9, 58, 22, tzinfo=datetime.timezone.utc))
        self.assertEqual(risk.load_prices(doc, "2026-10-29")[2], {})

    def test_quote_warns_when_yahoo_gives_no_previous_close(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "yahoo-TCS.json")) as fh:
                doc = json.load(fh)
            del doc["chart"]["result"][0]["meta"]["chartPreviousClose"]
            with open(os.path.join(d, "yahoo-TCS.json"), "w") as fh:
                json.dump(doc, fh)
            code, text, _ = run(["quote", "TCS", "--offline", d])
            self.assertEqual(code, 0)
            self.assertIn("no previous close for TCS", text)
        finally:
            shutil.rmtree(d)

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash": 0, "positions": [{"ticker": "TCS", "shares": 1}]}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", pf, "--plan", self.plan, "--offline", QDIR, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(text)["quotes"]), ["RELIANCE", "TCS"])

    def test_quote_unknown_symbol_is_an_error_line(self):
        code, text, _ = run(["quote", "ZZZZNOPE", "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("No data found", text)

    def test_clock_exit_codes(self):
        self.assertEqual(run(["clock", "--now", "2026-10-28 10:15"])[0], 0)
        code, text, _ = run(["clock", "--now", "2026-12-25 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("Christmas", text)
        self.assertIn("Next open: 2026-12-28 09:15 IST", text)
        code, text, _ = run(["clock", "--now", "2026-10-28 09:05"])
        self.assertIn("pre-open", text)
        code, text, _ = run(["clock", "--now", "2027-03-01 10:00"])
        self.assertIn("NSE calendar for 2027 not loaded", text)
        code, text, _ = run(["clock", "--now", "2026-10-28 10:00", "--json"])
        self.assertEqual(json.loads(text)["state"], "open")

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--broker", self.broker, "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)


class CalendarCommand(unittest.TestCase):
    def setUp(self):
        self.saved = (alerts._urlopen, alerts._sleep)

    def tearDown(self):
        alerts._urlopen, alerts._sleep = self.saved

    def feed(self):
        with open(os.path.join(QDIR, "holiday-master.json")) as fh:
            return json.load(fh)

    def offline_dir(self, doc):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(d, "holiday-master.json"), "w") as fh:
            json.dump(doc, fh)
        return d

    def test_plain_calendar_prints_the_loaded_table(self):
        code, text, _ = run(["calendar"])
        self.assertEqual(code, 0)
        self.assertIn("2026-01-26  Monday     Republic Day", text)
        self.assertIn("2026-11-10  Tuesday    Diwali-Balipratipada", text)
        self.assertIn("16 holidays", text)

    def test_refresh_offline_against_the_saved_feed_has_zero_diffs_for_2026(self):
        code, text, _ = run(["calendar", "--refresh", "--offline", QDIR, "--year", "2026"])
        self.assertEqual(code, 0)
        self.assertIn("NSE CM holidays for 2026 (weekdays only): 16", text)
        self.assertIn("Diwali-Balipratipada", text)
        self.assertNotIn("Mahashivratri", text)                      # a Sunday
        self.assertNotIn("Independence Day", text)                   # a Saturday
        self.assertIn("0 differences", text)
        self.assertIn("never edits", text)

    def test_refresh_reports_added_removed_and_renamed(self):
        doc = self.feed()
        cm = doc["CM"]
        cm.append({"tradingDate": "17-Dec-2026", "weekDay": "Thursday", "description": "Made Up Day"})
        cm[:] = [h for h in cm if h["tradingDate"] != "14-Sep-2026"]
        for h in cm:
            if h["tradingDate"] == "03-Mar-2026":
                h["description"] = "Holi (Dhulandi)"
        code, text, _ = run(["calendar", "--refresh", "--offline", self.offline_dir(doc), "--year", "2026"])
        self.assertEqual(code, 1)
        self.assertIn("3 differences", text)
        self.assertIn("added   2026-12-17  Made Up Day", text)
        self.assertIn("removed 2026-09-14  Ganesh Chaturthi", text)
        self.assertIn("renamed 2026-03-03  Holi -> Holi (Dhulandi)", text)
        self.assertIn("NSE_HOLIDAYS", text)

    def test_refresh_for_an_unloaded_year(self):
        doc = self.feed()
        doc["CM"].append({"tradingDate": "26-Jan-2027", "weekDay": "Tuesday", "description": "Republic Day"})
        code, text, _ = run(["calendar", "--refresh", "--offline", self.offline_dir(doc), "--year", "2027"])
        self.assertEqual(code, 1)
        self.assertIn("added   2027-01-26  Republic Day", text)
        self.assertIn("2027 is not in CALENDAR_YEARS", text)

    def test_refresh_fetches_the_url_with_the_user_agent(self):
        calls = []
        body = json.dumps(self.feed()).encode("utf-8")

        def fake(req, timeout=None):
            calls.append((req.full_url, req.get_header("User-agent")))
            return io.BytesIO(body)
        alerts._urlopen = fake
        code, text, _ = run(["calendar", "--refresh", "--year", "2026"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("https://www.nseindia.com/api/holiday-master?type=trading", UA)])

    def test_refresh_errors_are_plain(self):
        code, _, err = run(["calendar", "--refresh", "--offline", self.offline_dir({"FO": []}), "--year", "2026"])
        self.assertEqual(code, 1)
        self.assertIn("no CM list", err)

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "x", {}, io.BytesIO(b""))
        alerts._urlopen = boom
        code, _, err = run(["calendar", "--refresh"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", err)
        self.assertIn("nseindia.com", err)

    def test_the_fixture_is_the_cm_list_plus_one_other_key(self):
        doc = self.feed()
        self.assertEqual(sorted(doc), ["CBM", "CM"])
        self.assertEqual(len(doc["CM"]), 20)


class Docs(unittest.TestCase):
    def test_the_skill_text(self):
        with open(os.path.join(ROOT, "nse-alerts", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        low = " ".join(text.lower().split())
        for need in ("name: nse-alerts", "09:15", "orphan", "calendar --refresh", "quote reliance --write",
                     "not financial advice", "yes", "nse/prices.json", "ist"):
            self.assertIn(need, low, need)
        self.assertNotIn(chr(0x2014), text)
        self.assertNotIn("stooq", low)
        self.assertGreater(len(text.splitlines()), 60)


if __name__ == "__main__":
    unittest.main()
