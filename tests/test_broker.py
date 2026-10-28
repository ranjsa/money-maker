import copy
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("nse-broker", "ticket")
check = load("nse-risk", "check")

CFG_PATH = os.path.join(ROOT, "nse-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "nse-risk", "orders-demo.json")
GROUP = "DEMO-2026-10-27-1"
ENTRY_CALL = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "DEMO", "transaction_type": "BUY",
              "quantity": 73, "product": "MIS", "order_type": "MARKET", "validity": "DAY"}
STOP_CALL = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "DEMO", "transaction_type": "SELL",
             "quantity": 73, "product": "MIS", "order_type": "SL-M", "validity": "DAY", "trigger_price": 1005.2}
TARGET_CALL = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "DEMO", "transaction_type": "SELL",
               "quantity": 73, "product": "MIS", "order_type": "LIMIT", "price": 1034.6, "validity": "DAY"}


def demo(**over):
    with open(ORDERS_PATH, encoding="utf-8") as fh:
        g = json.load(fh)["orders"][0]
    g.update(over)
    return g


def other(symbol="RELIANCE", qty=20, group="RELIANCE-2026-10-27-1"):
    g = demo(group=group)
    for lg in g["legs"]:
        lg["tradingsymbol"], lg["quantity"] = symbol, qty
    g["plan"] = dict(g["plan"], entry=1450.0, stop=1440.0, target=1470.0, max_loss=250.0)
    g["legs"][1]["trigger_price"], g["legs"][2]["price"] = 1440.0, 1470.0
    return g


def real_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def ist_today():
    return ticket.to_ist(real_now()).date().isoformat()


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


def write_risk(path, groups, verdict="PASS", date=None, now_ist=None, checked_at=None, reasons=None):
    """nse/state/risk.json exactly as nse-risk writes it (check.write_verdicts), for these groups."""
    now = real_now()
    res = {"date": date or ist_today(),
           "now_ist": now_ist or ticket.to_ist(now).strftime("%Y-%m-%d %H:%M"),
           "paper": True, "pass": 0, "block": 0, "warnings": [], "cash_after": 0, "today": {},
           "results": [{"index": i, "group": g["group"], "symbol": g["legs"][0]["tradingsymbol"], "side": "BUY",
                        "kind": "entry", "qty": g["legs"][0]["quantity"], "legs": len(g["legs"]),
                        "max_loss": g["plan"]["max_loss"], "fingerprint": check.fingerprint(g),
                        "verdict": verdict, "reasons": reasons or [], "warnings": [], "info": []}
                       for i, g in enumerate(groups, 1)]}
    check.write_verdicts(path, res, "nse/orders.json")
    if checked_at is not None:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        for v in doc["verdicts"].values():
            v["checked_at"] = checked_at
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, group=None, batch=None):
        g = group or demo()
        return ticket.parse_yes(reply, g, batch if batch is not None else [g])[0]

    def test_a_yes_that_names_the_symbol_and_the_qty_counts(self):
        for r in ("yes 73 DEMO", "Yes, buy 73 DEMO.", "YES BUY 73 demo", "y 73 DEMO", "yes buy 73 DEMO",
                  "yes 73 DEMO max loss 788.66", "yes 73 DEMO target 1034.60", "yes 73 shares of DEMO"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "yes", "Yes.", "y", "yes DEMO", "yes 73", "yes buy it", "sure 73 DEMO", "ok 73 DEMO",
                  "go 73 DEMO", "yeah 73 DEMO", "yes to all", "yes, all of them 73 DEMO", "yes both 73 DEMO",
                  "yes 74 DEMO", "yes 73 DEMO?", "yes 73 DEMO but later", "no", "not yes 73 DEMO",
                  "yes 73 DEMO if it dips", "yes 73 DEMO, wait", "yes 73 DEMO at 1000"):
            self.assertFalse(self.yes(r), r)

    def test_a_two_group_batch(self):
        g1, g2 = demo(), other()
        batch = [g1, g2]
        self.assertFalse(self.yes("yes", g1, batch))
        self.assertTrue(self.yes("yes 73 DEMO", g1, batch))
        self.assertTrue(self.yes("yes 20 RELIANCE", g2, batch))
        ok, why = ticket.parse_yes("yes 73 DEMO RELIANCE", g1, batch)
        self.assertFalse(ok)
        self.assertIn("RELIANCE", why)
        self.assertFalse(self.yes("yes 20 RELIANCE", g1, batch))

    def test_two_identical_looking_groups_are_ambiguous(self):
        g1 = demo()
        g2 = demo(group="DEMO-2026-10-27-2")
        g2["legs"][2]["price"] = 1040.0
        g2["plan"] = dict(g2["plan"], target=1040.0)
        ok, why = ticket.parse_yes("yes 73 DEMO", g1, [g1, g2])
        self.assertFalse(ok)
        self.assertIn("ambiguous", why)
        self.assertTrue(self.yes("yes 73 DEMO target 1034.60", g1, [g1, g2]))

    def test_symbols_with_ampersand_and_hyphen(self):
        mm = other("M&M", 10, "MM-1")
        self.assertTrue(self.yes("yes 10 M&M", mm, [mm]))
        ba = other("BAJAJ-AUTO", 5, "BA-1")
        self.assertTrue(self.yes("yes 5 BAJAJ-AUTO", ba, [ba]))
        self.assertFalse(self.yes("yes 5 BAJAJ", ba, [ba]))


class TicketShape(unittest.TestCase):
    def test_tool_input_is_the_kite_place_order_call(self):
        g = demo()
        self.assertEqual(ticket.tool_input(g["legs"][0]), ENTRY_CALL)
        self.assertEqual(ticket.tool_input(g["legs"][1]), STOP_CALL)
        self.assertEqual(ticket.tool_input(g["legs"][2]), TARGET_CALL)
        for lg in g["legs"]:
            self.assertNotIn("role", ticket.tool_input(lg))
            self.assertTrue(set(ticket.tool_input(lg)) <= set(ticket.KITE_PLACE_ORDER))
        lim = dict(g["legs"][0], order_type="LIMIT", price="1015.50", quantity="73")
        call = ticket.tool_input(lim)
        self.assertEqual((call["price"], call["quantity"]), (1015.5, 73))

    def test_fingerprint_is_nse_risk_fingerprint(self):
        g = demo()
        self.assertEqual(ticket.fingerprint(g), check.fingerprint(g))
        self.assertEqual(ticket.fingerprint(demo(why="edited why")), check.fingerprint(g))
        g2 = demo()
        g2["legs"][0]["quantity"] = 74
        self.assertNotEqual(ticket.fingerprint(g2), check.fingerprint(g))

    def test_plain_words(self):
        g = demo()
        self.assertEqual(ticket.plain_leg(g["legs"][0]),
                         "BUY 73 DEMO on NSE at market, intraday (MIS), good for today")
        self.assertEqual(ticket.plain_leg(g["legs"][1]),
                         "SELL 73 DEMO on NSE, stop-loss at market (SL-M) triggered at ₹1,005.20, intraday (MIS), "
                         "good for today")
        self.assertEqual(ticket.plain_leg(g["legs"][2]),
                         "SELL 73 DEMO on NSE, limit at ₹1,034.60, intraday (MIS), good for today")
        self.assertEqual(ticket.max_loss(g), (788.66, 1005.2))


class DesktopConfig(unittest.TestCase):
    def test_example_has_hosted_and_self_hosted_kite_with_placeholders(self):
        with open(os.path.join(ROOT, "nse-broker", "claude_desktop_config.example.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        servers = doc["mcpServers"]
        self.assertEqual(servers["kite"]["args"], ["mcp-remote", "https://mcp.kite.trade/mcp"])
        local = servers["kite-self-hosted"]
        self.assertEqual(local["env"]["APP_MODE"], "stdio")
        for k in ("KITE_API_KEY", "KITE_API_SECRET"):
            self.assertIn("NEVER_IN_CHAT", local["env"][k])
        notes = " ".join(doc["_notes"])
        for word in ("place_order", "modify_order", "cancel_order", "Needs approval", "zerodha/kite-mcp-server"):
            self.assertIn(word, notes)

    def test_skill_records_the_verified_kite_tools(self):
        with open(os.path.join(ROOT, "nse-broker", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        for word in ("place_order", "modify_order", "cancel_order", "get_orders", "https://mcp.kite.trade/mcp",
                     "trigger_price", "tradingsymbol", "transaction_type", "Needs approval", "paper.py",
                     "name: nse-broker"):
            self.assertIn(word, text)


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def args(self, *extra):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state]

    def fill(self, *extra):
        return run(["fill", "--group", GROUP] + list(extra) + ["--orders", self.orders, "--config", self.cfg,
                                                               "--state", self.state, "--portfolio", self.pf])

    def set_broker(self, broker):
        with open(self.cfg, encoding="utf-8") as fh:
            c = json.load(fh)
        c["broker"] = broker
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump(c, fh)

    def state_doc(self):
        with open(self.state, encoding="utf-8") as fh:
            return json.load(fh)

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)
        self.assertNotIn("Reply \"yes", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def test_show_prints_three_legs_and_ready(self):
        write_risk(self.risk, [demo()])
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 0, text)
        self.assertIn("READY: nse-risk PASS", text)
        self.assertIn("PAPER", text)
        for n, role in ((1, "entry"), (2, "stop"), (3, "target")):
            self.assertIn("leg %d, %s:" % (n, role), text)
        self.assertEqual(text.count("place_order with exactly"), 3)
        self.assertIn('"trigger_price": 1005.2', text)
        self.assertIn('"price": 1034.6', text)
        self.assertNotIn('"role"', text)
        self.assertNotIn('"plan"', text)
        self.assertIn("only after the entry fills", text)
        self.assertIn("no bracket orders", text)
        self.assertIn("Not financial advice", text)
        self.assertIn('Reply "yes 73 DEMO"', text)
        code, text, _ = run(self.args("show", "--index", "1", "--json"))
        t = json.loads(text)
        self.assertEqual([x["tool_input"] for x in t["legs"]], [ENTRY_CALL, STOP_CALL, TARGET_CALL])
        self.assertTrue(t["ready"])

    def test_a_check_from_yesterday_needs_a_recheck(self):
        y = (datetime.date.fromisoformat(ist_today()) - datetime.timedelta(days=1)).isoformat()
        write_risk(self.risk, [demo()], date=y)
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("RECHECK", text)
        self.assertIn("run nse-risk again", text)

    def test_a_check_older_than_30_minutes_needs_a_recheck(self):
        old = (real_now() - datetime.timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_risk(self.risk, [demo()], checked_at=old,
                   now_ist=ticket.to_ist(real_now() - datetime.timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M"))
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("RECHECK", text)
        self.assertIn("run nse-risk again", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))[0], 1)

    def test_a_check_with_a_pinned_clock_needs_a_recheck(self):
        pinned = ticket.to_ist(real_now() - datetime.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
        write_risk(self.risk, [demo()], now_ist=pinned)
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("RECHECK", text)
        self.assertIn("pinned", text)

    def test_a_check_29_minutes_old_still_counts(self):
        t = real_now() - datetime.timedelta(minutes=29)
        write_risk(self.risk, [demo()], checked_at=t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   now_ist=ticket.to_ist(t).strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(run(self.args("show"))[0], 0)

    def test_an_edited_group_is_not_ready(self):
        write_risk(self.risk, [demo()])
        with open(self.orders, encoding="utf-8") as fh:
            doc = json.load(fh)
        for lg in doc["orders"][0]["legs"]:
            lg["quantity"] = 80
        with open(self.orders, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("not in today's risk check", text)

    def test_blocked_groups_are_never_ready(self):
        write_risk(self.risk, [demo()], verdict="BLOCK", reasons=["window: no new entries after 14:45 IST"])
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED by nse-risk: window: no new entries after 14:45 IST", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))[0], 1)

    def test_a_plain_yes_on_a_two_group_batch_is_refused(self):
        with open(self.orders, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["orders"].append(other())
        with open(self.orders, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        write_risk(self.risk, doc["orders"])
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists(self.state))
        code, text, _ = run(self.args("approve", "--index", "2", "--reply", "yes 73 DEMO"))
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.state))
        code, text, _ = run(self.args("approve", "--index", "2", "--reply", "yes 20 RELIANCE"))
        self.assertEqual(code, 0, text)

    def test_paper_approve_hands_the_group_to_the_paper_engine(self):
        write_risk(self.risk, [demo()])
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "sure"))
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.state))
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes buy 73 DEMO"))
        self.assertEqual(code, 0, text)
        self.assertIn("paper.py run --group %s" % GROUP, text)
        self.assertNotIn("place_order with exactly", text)
        rec = self.state_doc()["orders"][0]
        self.assertEqual((rec["status"], rec["reply"], rec["group"], rec["broker"], rec["kind"]),
                         ("approved", "yes buy 73 DEMO", GROUP, "paper", "entry"))
        self.assertEqual((rec["max_loss"], rec["qty"], rec["symbol"]), (788.66, 73, "DEMO"))
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes buy 73 DEMO"))
        self.assertEqual(code, 1)
        self.assertIn("APPROVED", err)

    def test_an_unknown_broker_is_an_error(self):
        self.set_broker("alpaca")
        write_risk(self.risk, [demo()])
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))
        self.assertEqual(code, 1)
        self.assertIn("broker", err)
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(run(self.args("show"))[0], 1)

    def test_the_kite_flow_entry_then_stop_and_target_then_cancel_the_sibling(self):
        self.set_broker("kite")
        write_risk(self.risk, [demo()])
        code, text, _ = run(self.args("show"))
        self.assertIn("REAL MONEY", text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))
        self.assertEqual(code, 0, text)
        first = text[text.index("{"):text.index("}") + 1]
        self.assertEqual(json.loads(first), ENTRY_CALL)
        self.assertIn("Needs approval", text)
        self.assertIn("after the entry fills", text)

        code, _, err = run(self.args("result", "--index", "1", "--leg", "stop", "--status", "placed",
                                     "--order-id", "222"))
        self.assertEqual(code, 1)
        self.assertIn("entry", err)
        code, text, _ = run(self.args("result", "--index", "1", "--leg", "entry", "--status", "placed",
                                      "--order-id", "111"))
        self.assertEqual(code, 0, text)
        code, text, _ = self.fill("--leg", "entry", "--price", "1015.00")
        self.assertEqual(code, 0, text)
        self.assertIn("place the stop-loss and the target now", text)
        self.assertIn('"trigger_price": 1005.2', text)
        run(self.args("result", "--index", "1", "--leg", "stop", "--status", "placed", "--order-id", "222"))
        run(self.args("result", "--index", "1", "--leg", "target", "--status", "placed", "--order-id", "333"))
        code, text, _ = self.fill("--leg", "target", "--price", "1034.60")
        self.assertEqual(code, 0, text)
        self.assertIn("cancel_order", text)
        call = json.loads(text[text.index("{"):text.rindex("}") + 1])
        self.assertEqual(call, {"variety": "regular", "order_id": "222"})
        self.assertIn("stop", text[:text.index("{")])

        st = self.state_doc()["orders"]
        self.assertEqual([r["status"] for r in st],
                         ["approved", "placed", "filled", "placed", "placed", "filled"])
        # nse-risk counts it: three Kite orders, one entry, its max loss, and DEMO BUY is a duplicate today
        today = ist_today()
        res = check.check([demo()], {"budget": 100000}, state={"orders": st}, today=today, now=real_now())
        self.assertEqual(res["today"], {"orders_placed": 3, "entries_placed": 1, "loss_at_risk": 788.66})
        self.assertIn("already placed today", " ".join(res["results"][0]["reasons"]))
        with open(self.pf, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["positions"], [])

    def test_fill_without_an_order_id_prints_a_placeholder(self):
        write_risk(self.risk, [demo()])
        self.fill("--leg", "entry", "--price", "1015.00")
        code, text, _ = self.fill("--leg", "stop", "--price", "1005.20")
        self.assertEqual(code, 0, text)
        self.assertIn('"order_id": "<order_id of the target leg>"', text)
        self.assertIn("no order id", text)
        self.assertIn("cancel_order", text)

    def test_result_needs_an_approval_and_decline_is_logged(self):
        write_risk(self.risk, [demo()])
        code, _, err = run(self.args("result", "--index", "1", "--leg", "entry", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        code, text, _ = run(self.args("decline", "--index", "1", "--reply", "no thanks"))
        self.assertEqual(code, 0)
        code, text, _ = run(self.args("show"))
        self.assertIn("DONE: declined", text)
        code, text, _ = run(["log", "--state", self.state])
        self.assertIn("declined", text)

    def test_flatten_writes_an_exit_group(self):
        code, text, _ = run(["flatten", "--symbol", "demo", "--qty", "73", "--orders", self.orders,
                             "--portfolio", self.pf, "--config", self.cfg])
        self.assertEqual(code, 0, text)
        self.assertIn("cancel_order", text)
        self.assertIn("15:10", text)
        with open(self.orders, encoding="utf-8") as fh:
            g = json.load(fh)["orders"][0]
        self.assertEqual(ticket.tool_input(g["legs"][0]),
                         {"variety": "regular", "exchange": "NSE", "tradingsymbol": "DEMO",
                          "transaction_type": "SELL", "quantity": 73, "product": "MIS", "order_type": "MARKET",
                          "validity": "DAY"})
        self.assertEqual(run(["flatten", "--symbol", "DEMO", "--qty", "0", "--orders", self.orders])[0], 1)
        code, _, err = run(["flatten", "--symbol", "DEMO", "--orders", self.orders, "--portfolio", self.pf])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)


if __name__ == "__main__":
    unittest.main()
