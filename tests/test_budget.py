import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, ROOT, FIX

size = load("nse-budget", "size")

CFG = os.path.join(ROOT, "nse-budget", "config.example.json")
PLAN = os.path.join(FIX, "nse-budget", "plan-demo.json")
NOW = "2026-10-27 10:15"
TODAY = "2026-10-27"
ENTRY, STOP, TARGET, RISK = Decimal("1015.00"), Decimal("1005.20"), Decimal("1034.60"), Decimal("9.80")


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def loose(**over):
    """Budget 1,00,000 with the caps out of the way, so the max loss is the limit that decides."""
    c = cfg(cash_buffer_pct=0, max_position_pct=100, max_order_inr=100000)
    c.update(over)
    return c


def plan(**over):
    with open(PLAN) as fh:
        p = json.load(fh)
    p.update(over)
    return p


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


def net_loss(qty, entry=ENTRY, stop=STOP):
    return qty * (entry - stop) + size.round_trip(qty, entry, stop)


def prices_doc(price, quote_time="2026-10-27T04:40:00Z"):
    return {"as_of": quote_time, "source": "yahoo", "prices": {"DEMO": price},
            "detail": {"DEMO": {"prev_close": 1000.0, "time": quote_time, "source": "yahoo"}}}


class ConfigExample(unittest.TestCase):
    def test_example_has_nse_defaults(self):
        with open(CFG) as fh:
            c = json.load(fh)
        self.assertEqual(c["budget"], 100000)
        self.assertEqual(c["max_loss_per_trade_inr"], 1000)
        self.assertEqual(c["max_order_inr"], 80000)
        self.assertEqual((c["product"], c["exchange"], c["broker"]), ("MIS", "NSE", "paper"))
        self.assertFalse([k for k in c if k.endswith("_usd")])


class SizingNetOfCharges(unittest.TestCase):
    def test_default_caps_bind_before_the_max_loss(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (73, "position cap"))        # 75000 / 1015

    def test_max_loss_binds_and_charges_are_inside_the_limit(self):
        s = size.size_trade(plan(), loose(), today=TODAY)
        q = s["qty"]
        self.assertEqual(s["bound_by"], "max loss")
        self.assertLessEqual(net_loss(q), 1000)
        self.assertGreater(net_loss(q + 1), 1000)          # one more share would break the cap
        self.assertGreater(q, 0)
        self.assertLess(q, 1000 // 9.80 + 1)                # charges cost at least a share
        self.assertEqual(s["charges"], float(size.round_trip(q, ENTRY, STOP)))
        self.assertEqual(s["max_loss"], float(round(net_loss(q), 2)))
        self.assertEqual(s["risk_per_share"], 9.80)
        self.assertEqual(s["reward_risk"], 2.0)
        self.assertIsNone(s["refusal"])

    def test_max_order_binds_at_floor_50000_over_1015(self):
        s = size.size_trade(plan(), loose(max_order_inr=50000), today=TODAY)
        self.assertEqual(s["qty"], 49)
        self.assertEqual(s["bound_by"], "max order")
        self.assertEqual([l["name"] for l in s["limits"] if l["binds"]], ["max order"])

    def test_position_cap_and_cash_can_bind(self):
        s = size.size_trade(plan(), cfg(max_position_pct=30), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (29, "position cap"))          # 30000 / 1015
        s = size.size_trade(plan(), cfg(budget=60000, max_loss_per_trade_inr=5000, max_position_pct=100,
                                        max_order_inr=80000), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (53, "cash"))                  # 54000 / 1015

    def test_cash_cap_from_portfolio(self):
        s = size.size_trade(plan(), loose(max_loss_per_trade_inr=5000, cash_buffer_pct=10), cash=30000, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (19, "cash"))                  # (30000 - 10000 buffer) / 1015

    def test_stop_too_far_is_a_plain_refusal(self):
        p = plan(entry=1215.00, stop=15.00, target=2500.0)             # risk 1200 a share
        s = size.size_trade(p, cfg(), today=TODAY)
        self.assertEqual(s["qty"], 0)
        need = Decimal("1200") + size.round_trip(1, Decimal("1215.00"), Decimal("15.00"))
        self.assertIn("a stop ₹1,200.00 away needs max loss ≥ " + size.inr(need), s["refusal"])
        self.assertIsNone(size.kite_order(s))

    def test_never_fractional(self):
        for ml in (700, 999, 1000, 1234.5):
            s = size.size_trade(plan(), loose(budget=200000, max_order_inr=200000, max_loss_per_trade_inr=ml), today=TODAY)
            self.assertIsInstance(s["qty"], int)
            self.assertLessEqual(net_loss(s["qty"]), Decimal(str(ml)))

    def test_charges_eat_a_tight_stop(self):
        # 180 stock, 0.40 stop: charges are a real part of the risk and cost shares
        p = plan(entry=180.00, stop=179.60, target=181.00, risk_per_share=0.40)
        s2 = size.size_trade(p, cfg(max_loss_per_trade_inr=100), today=TODAY)
        self.assertLess(s2["qty"], 250)                                  # 100 / 0.40 = 250 before charges
        self.assertLessEqual(net_loss(s2["qty"], Decimal("180.00"), Decimal("179.60")), 100)

    def test_custom_charges_override(self):
        free = size.size_trade(plan(), loose(budget=200000, max_order_inr=200000, charges={"brokerage_flat": 0, "brokerage_pct": 0, "stt_sell_pct": 0,
                                                    "txn_pct": 0, "sebi_per_crore": 0, "stamp_buy_pct": 0,
                                                    "gst_pct": 0}), today=TODAY)
        self.assertEqual(free["charges"], 0.0)
        self.assertEqual(free["qty"], 102)                               # floor(1000 / 9.80)

    def test_min_order_and_min_reward_risk_refuse(self):
        s = size.size_trade(plan(), cfg(min_order_inr=200000, max_order_inr=0), today=TODAY)
        self.assertIn("minimum order", s["refusal"])
        s = size.size_trade(plan(target=1020.0), cfg(), today=TODAY)
        self.assertIn("reward:risk", s["refusal"])

    def test_bad_plans(self):
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(stop=1020.0), cfg(), today=TODAY)
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(target=1010.0), cfg(), today=TODAY)
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(), cfg(), today="2026-10-28")
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(side="sell"), cfg(), today=TODAY)
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(), cfg(blocked=["demo"]), today=TODAY)
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(), cfg(budget=None), today=TODAY)

    def test_fresh_higher_price_means_bigger_risk(self):
        base = size.size_trade(plan(), loose(), today=TODAY)
        s = size.size_trade(plan(), loose(), price=1020.0, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"]), (1020.0, "price"))
        self.assertLessEqual(s["qty"], base["qty"])
        s = size.size_trade(plan(), loose(), price=1010.0, today=TODAY)
        self.assertEqual(s["entry_from"], "plan")


class KiteOrder(unittest.TestCase):
    def order(self, **kw):
        return size.kite_order(size.size_trade(plan(), cfg(), today=TODAY, **kw))

    def test_three_legs_in_the_exact_shape(self):
        s = size.size_trade(plan(), loose(), today=TODAY)
        o = size.kite_order(s)
        n = s["qty"]
        self.assertEqual(o["group"], "DEMO-2026-10-27-1")
        self.assertEqual(o["legs"], [
            {"role": "entry", "exchange": "NSE", "tradingsymbol": "DEMO", "transaction_type": "BUY",
             "quantity": n, "product": "MIS", "order_type": "MARKET", "variety": "regular", "validity": "DAY"},
            {"role": "stop", "exchange": "NSE", "tradingsymbol": "DEMO", "transaction_type": "SELL",
             "quantity": n, "product": "MIS", "order_type": "SL-M", "trigger_price": 1005.20,
             "variety": "regular", "validity": "DAY"},
            {"role": "target", "exchange": "NSE", "tradingsymbol": "DEMO", "transaction_type": "SELL",
             "quantity": n, "product": "MIS", "order_type": "LIMIT", "price": 1034.60,
             "variety": "regular", "validity": "DAY"}])
        self.assertIsInstance(o["legs"][0]["quantity"], int)
        self.assertEqual(o["plan"]["max_loss"], s["max_loss"])
        self.assertEqual(o["plan"]["charges"], s["charges"])
        self.assertEqual(o["plan"]["flat_by"], "15:10")
        self.assertIn("max loss per trade", o["why"])

    def test_limit_entry_carries_its_price(self):
        s = size.size_trade(plan(), loose(), limit="1014.50", today=TODAY)
        o = size.kite_order(s)
        e = o["legs"][0]
        self.assertEqual((e["order_type"], e["price"]), ("LIMIT", 1014.50))
        self.assertEqual(s["risk_per_share"], 9.30)


class Cli(unittest.TestCase):
    def test_writes_orders_json_and_numbers_groups(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "nse", "orders.json")
            argv = ["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                    "--now", NOW, "--write", out]
            code, text, _ = run(argv)
            self.assertEqual(code, 0)
            self.assertIn("₹", text)
            self.assertIn("Not financial advice".lower(), text.lower())
            self.assertIn("nothing has been placed", text)
            with open(out) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["created_by"], "nse-budget")
            self.assertEqual(doc["status"], "proposed")
            self.assertIs(doc["paper"], True)
            self.assertEqual(doc["created_at"], "2026-10-27T04:45:00Z")
            self.assertIn("nse-risk", doc["note"])
            o = doc["orders"][0]
            self.assertEqual(o["group"], "DEMO-2026-10-27-1")
            self.assertEqual(len({l["quantity"] for l in o["legs"]}), 1)
            run(argv)
            with open(out) as fh:
                self.assertEqual(json.load(fh)["orders"][0]["group"], "DEMO-2026-10-27-2")

    def test_refusal_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(plan(entry=1215.0, stop=15.0, target=2500.0), fh)
            out = os.path.join(d, "orders.json")
            code, text, _ = run(["--plan", p, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 1)
            self.assertIn("No order", text)
            self.assertFalse(os.path.exists(out))

    def test_json_and_limit(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW,
                             "--limit", "--json"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(out["orders"][0]["legs"][0]["order_type"], "LIMIT")
        self.assertEqual(out["orders"][0]["legs"][0]["price"], 1015.0)
        self.assertEqual(out["sizing"]["qty"], out["orders"][0]["legs"][1]["quantity"])

    def test_prices_file_used_only_when_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump(prices_doc(1020.0), fh)           # 04:40Z = 10:10 IST, now is 10:15 IST
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW,
                                 "--prices", pp, "--json"])
            out = json.loads(text)
            self.assertEqual(out["sizing"]["entry_from"], "price")
            self.assertTrue(out["notes"])
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent",
                                 "--now", "2026-10-27 11:30", "--prices", pp, "--json"])
            out = json.loads(text)
            self.assertEqual(out["sizing"]["entry_from"], "plan")
            self.assertIn("not used", out["notes"][0])

    def test_wrong_day_and_missing_plan(self):
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", "2026-10-28 10:15"])
        self.assertEqual(code, 1)
        self.assertIn("nse-exit", err)
        code, _, err = run(["--plan", "/nonexistent/plan.json", "--config", CFG, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)

    def test_weights_mode_is_gone(self):
        with self.assertRaises(SystemExit):
            run(["--weights", "A=1"])
        self.assertFalse(hasattr(size, "run_weights"))
        self.assertFalse(os.path.exists(os.path.join(ROOT, "nse-budget", "targets.example.json")))


class SkillMd(unittest.TestCase):
    def test_skill_copy(self):
        with open(os.path.join(ROOT, "nse-budget", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("name: nse-budget", text)
        self.assertNotIn(chr(0x2014), text)
        self.assertGreater(len(text.splitlines()), 60)
        for w in ("₹", "MIS", "Not financial advice", "yes", "SL-M", "no bracket"):
            self.assertIn(w, text)
        for bad in ("trader-", "Alpaca", "--weights", "New York"):
            self.assertNotIn(bad, text)
        self.assertNotRegex(text, r"\$\d")


if __name__ == "__main__":
    unittest.main()
