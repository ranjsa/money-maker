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


# The "real" clock the Flow tests run on: Tue 2026-10-27 10:30 IST, inside the entry window, so the
# tests do not depend on the hour they run at. Flow.setUp pins ticket.real_now to it.
FAKE_NOW = datetime.datetime(2026, 10, 27, 5, 0, tzinfo=datetime.timezone.utc)


def real_now():
    return FAKE_NOW


def ist_today():
    return ticket.to_ist(real_now()).date().isoformat()


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


def write_risk(path, groups, verdict="PASS", date=None, now_ist=None, checked_at=None, reasons=None, kind="entry",
               max_loss="plan"):
    """nse/state/risk.json exactly as nse-risk writes it (check.write_verdicts), for these groups."""
    now = real_now()
    res = {"date": date or ist_today(),
           "now_ist": now_ist or ticket.to_ist(now).strftime("%Y-%m-%d %H:%M"),
           "paper": True, "pass": 0, "block": 0, "warnings": [], "cash_after": 0, "today": {},
           "results": [{"index": i, "group": g["group"], "symbol": g["legs"][0]["tradingsymbol"], "side": "BUY",
                        "kind": kind, "qty": g["legs"][0]["quantity"], "legs": len(g["legs"]),
                        "max_loss": g["plan"].get("max_loss") if max_loss == "plan" else max_loss,
                        "fingerprint": check.fingerprint(g),
                        "verdict": verdict, "reasons": reasons or [], "warnings": [], "info": []}
                       for i, g in enumerate(groups, 1)]}
    check.write_verdicts(path, res, "nse/orders.json")
    # write_verdicts stamps the machine clock; restamp this run (top level and its verdicts) on the
    # pinned test clock, keeping verdicts merged from earlier runs as they were
    stamp = checked_at or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    fps = {check.fingerprint(g) for g in groups}
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["checked_at"] = stamp
    for fp, v in doc["verdicts"].items():
        if fp in fps:
            v["checked_at"] = stamp
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

    def test_nevermind_and_nvm_are_doubts(self):
        for r in ("yes 73 DEMO nevermind", "yes 73 DEMO, nvm", "Yes 73 DEMO... NVM", "yes nevermind 73 DEMO"):
            g = demo()
            ok, why = ticket.parse_yes(r, g, [g])
            self.assertFalse(ok, r)
            self.assertIn("doubt", why, r)

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

    def test_retractions_and_doubts_are_not_a_yes(self):
        # I4: every doubt word, alone and in the phrasings people use to take a yes back
        words = ("never", "mind", "scratch", "actually", "nah", "nope", "hmm", "hang", "abort", "forget",
                 "cancel", "wait", "no", "not", "dont", "don't", "but", "maybe", "later", "hold", "unless", "if")
        for w in words:
            self.assertFalse(self.yes("yes 73 DEMO %s" % w), w)
        mm = other("M&M", 10, "MM-1")
        for r in ("yes 10 M&M never mind", "yes 10 M&M scratch that", "yes 10 M&M nah", "yes 10 M&M actually no",
                  "yes 10 M&M nope", "yes 10 M&M hmm", "yes 10 M&M hang on", "yes 10 M&M abort",
                  "yes 10 M&M forget it", "yes 10 M&M cancel", "yes 10 M&M wait"):
            self.assertFalse(self.yes(r, mm, [mm]), r)
        self.assertTrue(self.yes("yes 10 M&M", mm, [mm]))

    def test_stop_names_the_stop_price_to_pick_between_two_groups(self):
        # I4: "stop" is not a doubt word: it picks the group by its stop price
        g1 = demo()
        g2 = demo(group="DEMO-2026-10-27-2")
        g2["legs"][1]["trigger_price"] = 1004.0
        g2["plan"] = dict(g2["plan"], stop=1004.0, max_loss=None)
        ok, why = ticket.parse_yes("yes 73 DEMO stop 1005.2", g1, [g1, g2])
        self.assertTrue(ok, why)
        self.assertFalse(self.yes("yes 73 DEMO stop 1005.2", g2, [g1, g2]))
        self.assertTrue(self.yes("yes 73 DEMO stop 1004", g2, [g1, g2]))

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
        self.saved_now = ticket.real_now
        ticket.real_now = real_now

    def tearDown(self):
        ticket.real_now = self.saved_now
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
        c["paper"] = broker != "kite"   # the two keys must agree
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

    def approve_kite(self):
        self.set_broker("kite")
        write_risk(self.risk, [demo()])
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))
        self.assertEqual(code, 0, text)

    def test_fill_without_an_approval_is_refused(self):
        self.set_broker("kite")
        code, text, err = self.fill("--leg", "entry", "--price", "1015")
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        self.assertNotIn("place_order", text)
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(os.path.exists(self.pf))

    def test_a_stop_or_target_fill_needs_the_entry_fill_first(self):
        self.approve_kite()
        for lg in ("stop", "target"):
            code, text, err = self.fill("--leg", lg, "--price", "1005.20")
            self.assertEqual(code, 1, lg)
            self.assertIn("entry leg has no fill", err)
            self.assertNotIn("cancel_order", text)
        self.assertFalse(os.path.exists(self.pf))

    def test_a_fill_qty_above_the_leg_is_refused(self):
        self.approve_kite()
        code, text, err = self.fill("--leg", "entry", "--price", "1015", "--qty", "730")
        self.assertEqual(code, 1)
        self.assertIn("more than the entry leg's quantity 73", err)
        self.assertNotIn("730", text)
        self.assertFalse(os.path.exists(self.pf))
        self.assertEqual(self.fill("--leg", "entry", "--price", "1015", "--qty", "50")[0], 0)
        code, _, err = self.fill("--leg", "stop", "--price", "1005.20", "--qty", "60")
        self.assertEqual(code, 1)
        self.assertIn("50 shares the entry fill left open", err)

    def test_paper_mode_fill_leaves_the_portfolio_alone(self):
        write_risk(self.risk, [demo()])
        run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))
        code, text, _ = self.fill("--leg", "entry", "--price", "1015.00")
        self.assertEqual(code, 0, text)
        self.assertFalse(os.path.exists(self.pf))
        self.assertEqual(self.state_doc()["orders"][-1]["status"], "filled")

    def test_a_check_with_no_usable_time_needs_a_recheck(self):
        for bad in (None, "garbage"):
            write_risk(self.risk, [demo()])
            with open(self.risk, encoding="utf-8") as fh:
                doc = json.load(fh)
            for rec in [doc] + list(doc["verdicts"].values()):
                for k in ("checked_at", "now_ist"):
                    if bad is None:
                        rec.pop(k, None)
                    else:
                        rec[k] = bad
            with open(self.risk, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            code, text, _ = run(self.args("show"))
            self.assertEqual(code, 1, bad)
            self.assertIn("RECHECK: the risk check has no usable time: run nse-risk again", text)
            self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))[0], 1)

    def test_fill_without_an_order_id_prints_a_placeholder(self):
        write_risk(self.risk, [demo()])
        run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))
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

    # ------------------------------------------------ I1: a PASS from an earlier run, or before a trade
    def two_groups(self):
        with open(self.orders, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["orders"].append(other())
        with open(self.orders, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return doc["orders"]

    def stamp(self, minutes):
        t = FAKE_NOW + datetime.timedelta(minutes=minutes)
        return t.strftime("%Y-%m-%dT%H:%M:%SZ"), ticket.to_ist(t).strftime("%Y-%m-%d %H:%M")

    def test_a_pass_from_an_earlier_run_is_not_ready(self):
        a, b = demo(), other()
        at, ist = self.stamp(-10)
        write_risk(self.risk, [a], checked_at=at, now_ist=ist)
        at, ist = self.stamp(-5)
        write_risk(self.risk, [b], checked_at=at, now_ist=ist)       # the latest run checked only B
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1, text)
        self.assertIn("RECHECK", text)
        self.assertIn("earlier risk run", text)
        self.assertIn("run nse-risk again", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.state))

    def test_a_pass_from_before_another_trade_is_not_ready(self):
        # 10:20 both pass; the user takes RELIANCE at 10:30; DEMO's 10:20 PASS no longer counts
        groups = self.two_groups()
        at, ist = self.stamp(-10)
        write_risk(self.risk, groups, checked_at=at, now_ist=ist)
        code, text, _ = run(self.args("approve", "--index", "2", "--reply", "yes 20 RELIANCE"))
        self.assertEqual(code, 0, text)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1, text)
        self.assertIn("RECHECK", text)
        self.assertIn("after this risk check", text)
        self.assertNotIn("READY: nse-risk PASS", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))
        self.assertEqual(code, 1)
        self.assertIn("RECHECK", err)
        # a fresh run after the trade counts again
        ticket.real_now = lambda: FAKE_NOW + datetime.timedelta(minutes=2)
        at, ist = self.stamp(1)
        write_risk(self.risk, groups, checked_at=at, now_ist=ist)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0, text)
        self.assertIn("READY: nse-risk PASS", text)

    def test_a_paper_placed_record_after_the_check_is_not_ready(self):
        write_risk(self.risk, [demo()], checked_at=self.stamp(-10)[0], now_ist=self.stamp(-10)[1])
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump({"orders": [{"at": ticket.ist_iso(FAKE_NOW - datetime.timedelta(minutes=5)),
                                   "date": ist_today(), "status": "placed", "group": "X-1", "fingerprint": "x",
                                   "symbol": "X", "side": "BUY", "kind": "entry", "max_loss": 300.0}]}, fh)
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1, text)
        self.assertIn("after this risk check", text)

    # ------------------------------------------------ I2: the entry window on the real clock
    def at_clock(self, ist_text):
        t = ticket.parse_now(ist_text)
        ticket.real_now = lambda: t
        return t

    def test_no_entry_is_ready_after_no_entries_after(self):
        t = self.at_clock("2026-10-27 14:50")
        write_risk(self.risk, [demo()], checked_at=t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   now_ist="2026-10-27 14:44")
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1, text)
        self.assertIn("no new entries after 14:45 IST (it is 14:50)", text)
        self.assertNotIn("READY: nse-risk PASS", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))
        self.assertEqual(code, 1)
        self.assertIn("no new entries after 14:45", err)
        self.assertFalse(os.path.exists(self.state))

    def test_no_entry_is_ready_before_no_entries_before_or_when_closed(self):
        for clock, want in (("2026-10-27 09:20", "no new entries before 09:30 IST"),
                            ("2026-10-31 10:30", "NSE is closed"),
                            ("2026-10-27 15:40", "NSE is closed")):
            t = self.at_clock(clock)
            write_risk(self.risk, [demo()], date=clock[:10], checked_at=t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       now_ist=clock)
            code, text, _ = run(self.args("show"))
            self.assertEqual(code, 1, clock)
            self.assertIn(want, text, clock)
            self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))[0], 1, clock)
            self.assertFalse(os.path.exists(self.state))

    def test_an_exit_is_ready_after_the_entry_window(self):
        t = self.at_clock("2026-10-27 15:05")
        run(["flatten", "--symbol", "DEMO", "--qty", "73", "--orders", self.orders, "--portfolio", self.pf,
             "--config", self.cfg])
        with open(self.orders, encoding="utf-8") as fh:
            g = json.load(fh)["orders"][0]
        g["plan"] = {"max_loss": None}
        write_risk(self.risk, [g], kind="exit", checked_at=t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   now_ist="2026-10-27 15:05")
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 0, text)
        self.assertIn("READY: nse-risk PASS", text)

    # ------------------------------------------------ I3: the yes is stamped on the real clock
    def test_approve_refuses_a_pinned_clock(self):
        write_risk(self.risk, [demo()])
        code, text, err = run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO",
                                        "--now", "2026-10-27 10:30"))
        self.assertEqual(code, 1)
        self.assertIn("--now", err)
        self.assertFalse(os.path.exists(self.state))
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))
        self.assertEqual(code, 0, text)
        self.assertEqual(self.state_doc()["orders"][0]["at"], "2026-10-27T10:30:00+05:30")

    # ------------------------------------------------ Gap 1: every record is stamped on the real clock
    def test_result_fill_and_decline_refuse_a_pinned_clock(self):
        self.set_broker("kite")
        write_risk(self.risk, [demo()])
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))[0], 0)
        with open(self.state, encoding="utf-8") as fh:
            before = fh.read()
        for argv in (self.args("result", "--index", "1", "--leg", "entry", "--status", "placed",
                               "--now", "2026-10-27 10:21"),
                     self.args("decline", "--index", "1", "--reply", "no", "--now", "2026-10-27 10:21"),
                     ["fill", "--group", GROUP, "--leg", "entry", "--price", "1015", "--orders", self.orders,
                      "--config", self.cfg, "--state", self.state, "--portfolio", self.pf,
                      "--now", "2026-10-27 10:21"]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("--now", err, argv)
            self.assertIn("real clock", err, argv)
            with open(self.state, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), before, argv)
        self.assertFalse(os.path.exists(self.pf))

    def test_a_backdated_placement_cannot_hide_from_the_stale_check(self):
        # 10:20 approve A (RELIANCE); 10:25 nse-risk; 10:30 result A --now 10:21 is refused
        groups = self.two_groups()
        self.set_broker("kite")
        t = self.at_clock("2026-10-27 10:20")
        write_risk(self.risk, groups, checked_at=t.strftime("%Y-%m-%dT%H:%M:%SZ"), now_ist="2026-10-27 10:20")
        self.assertEqual(run(self.args("approve", "--index", "2", "--reply", "yes 20 RELIANCE"))[0], 0)
        t = self.at_clock("2026-10-27 10:25")
        write_risk(self.risk, groups, checked_at=t.strftime("%Y-%m-%dT%H:%M:%SZ"), now_ist="2026-10-27 10:25")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("READY: nse-risk PASS at 10:25", text)
        self.at_clock("2026-10-27 10:30")
        code, _, err = run(self.args("result", "--index", "2", "--leg", "entry", "--status", "placed",
                                     "--now", "2026-10-27 10:21"))
        self.assertEqual(code, 1)
        self.assertIn("--now", err)
        self.assertEqual([r["status"] for r in self.state_doc()["orders"]], ["approved"])
        # the same placement on the real clock makes B's 10:25 PASS stale
        code, text, err = run(self.args("result", "--index", "2", "--leg", "entry", "--status", "placed"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.state_doc()["orders"][-1]["at"], "2026-10-27T10:30:00+05:30")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1, text)
        self.assertIn("RECHECK", text)
        self.assertNotIn("READY: nse-risk PASS", text)

    def test_an_approval_carries_its_leg_count_and_value(self):
        write_risk(self.risk, [demo()])
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))[0], 0)
        rec = self.state_doc()["orders"][0]
        self.assertEqual((rec["legs"], rec["value_inr"]), (3, 74095.0))

    # ------------------------------------------------ Gap 2: nse-risk counts an approval not yet placed
    def test_the_recheck_after_an_approval_counts_it(self):
        groups = self.two_groups()
        write_risk(self.risk, groups)
        self.assertEqual(run(self.args("approve", "--index", "2", "--reply", "yes 20 RELIANCE"))[0], 0)
        self.assertIn("RECHECK", run(self.args("show", "--index", "1"))[1])
        c = {"budget": 1000000, "max_trades_per_day": 1}
        res = check.check([groups[0]], c, state=self.state_doc(), today=ist_today(), now=real_now())
        # (no price bands here, so the circuit check blocks too: the trades reason is the one under test)
        self.assertIn("trades: 1 entries placed today", " ".join(res["results"][0]["reasons"]))
        self.assertEqual(res["today"]["loss_at_risk"], 250.0)
        run(self.args("decline", "--index", "2", "--reply", "no"))
        res = check.check([groups[0]], c, state=self.state_doc(), today=ist_today(), now=real_now())
        self.assertEqual(res["today"]["entries_placed"], 0)

    # ------------------------------------------------ I5: broker and paper must agree
    def set_cfg(self, **over):
        with open(self.cfg, encoding="utf-8") as fh:
            c = json.load(fh)
        c.update(over)
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump(c, fh)

    def test_broker_and_paper_that_disagree_are_a_config_error(self):
        write_risk(self.risk, [demo()])
        for broker, paper in (("kite", True), ("paper", False)):
            self.set_cfg(broker=broker, paper=paper)
            for argv in (self.args("show"), self.args("approve", "--index", "1", "--reply", "yes 73 DEMO")):
                code, text, err = run(argv)
                self.assertEqual(code, 1, (broker, paper))
                self.assertIn('"broker"', err)
                self.assertIn('"paper"', err)
                self.assertNotIn("READY", text)
            self.assertFalse(os.path.exists(self.state))

    # ------------------------------------------------ I6: the logged max loss is nse-risk's measured one
    def test_the_placed_max_loss_is_the_risk_verdicts(self):
        self.set_broker("kite")
        write_risk(self.risk, [demo()], max_loss=812.4)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))
        self.assertEqual(code, 0, text)
        self.assertEqual(self.state_doc()["orders"][0]["max_loss"], 812.4)
        run(self.args("result", "--index", "1", "--leg", "entry", "--status", "placed", "--order-id", "111"))
        placed = [r for r in self.state_doc()["orders"] if r["status"] == "placed"][0]
        self.assertEqual(placed["max_loss"], 812.4)
        res = check.check([], {"budget": 100000}, state=self.state_doc(), today=ist_today(), now=real_now())
        self.assertEqual(res["today"]["loss_at_risk"], 812.4)

    def test_without_a_measured_max_loss_the_plan_is_used_with_a_warning(self):
        write_risk(self.risk, [demo()], max_loss=None)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes 73 DEMO"))
        self.assertEqual(code, 0, text)
        self.assertEqual(self.state_doc()["orders"][0]["max_loss"], 788.66)
        self.assertIn("WARNING", text)
        self.assertIn("plan", text)


if __name__ == "__main__":
    unittest.main()
