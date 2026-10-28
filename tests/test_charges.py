import unittest
from decimal import Decimal as D

from _load import load

ex = load("nse-exit", "exit")
charges, round_trip, DEF = ex.charges, ex.round_trip, ex.CHARGES_DEFAULT


class TestCharges(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(DEF["brokerage_flat"], 20)
        self.assertEqual(D(str(DEF["txn_pct"])), D("0.00307"))
        self.assertEqual(DEF["gst_pct"], 18)

    def test_round_trip_example(self):
        c = charges(D("100000"), D("101000"))
        self.assertEqual(c["brokerage"], D("40.00"))
        self.assertEqual(c["stt"], D("25.25"))
        self.assertEqual(c["stamp"], D("3.00"))
        self.assertEqual(c["txn"], D("6.17"))   # 0.00307% of 201000 = 6.1707
        self.assertEqual(c["sebi"], D("0.20"))  # 10 per crore of 201000 = 0.201
        self.assertEqual(c["total"], c["brokerage"] + c["stt"] + c["txn"] + c["sebi"] + c["stamp"] + c["gst"])

    def test_gst_is_18pct_of_brokerage_txn_sebi(self):
        c = charges(D("100000"), D("101000"))
        self.assertEqual(c["gst"], ((c["brokerage"] + c["txn"] + c["sebi"]) * D("0.18")).quantize(D("0.01"), "ROUND_HALF_UP"))
        self.assertEqual(c["gst"], D("8.35"))

    def test_small_order_uses_pct_brokerage(self):
        self.assertEqual(charges(D("5000"), D("5000"))["brokerage"], D("3.00"))

    def test_zero_override(self):
        c = charges(D("1000"), D("1000"), {"brokerage_flat": 0, "brokerage_pct": 0})
        self.assertEqual(c["brokerage"], D("0.00"))

    def test_zero_sell_side(self):
        c = charges(D("100000"), D("0"))
        self.assertEqual(c["brokerage"], D("20.00"))
        self.assertEqual(c["stt"], D("0.00"))
        self.assertEqual(c["stamp"], D("3.00"))
        self.assertEqual(c["total"], c["brokerage"] + c["stt"] + c["txn"] + c["sebi"] + c["stamp"] + c["gst"])

    def test_inputs_and_types(self):
        c = charges(100000, "101000.0")
        self.assertEqual(c, charges(D("100000"), D("101000")))
        self.assertEqual(charges(100000.5, 0)["stamp"], D("3.00"))
        for v in c.values():
            self.assertIsInstance(v, D)

    def test_partial_override(self):
        c = charges(D("100000"), D("100000"), {"brokerage_flat": 10})
        self.assertEqual(c["brokerage"], D("20.00"))

    def test_round_trip_fn(self):
        self.assertEqual(round_trip(100, D("1000"), D("1010")), charges(D("100000"), D("101000"))["total"])
        self.assertEqual(round_trip(100, 1000, 1010, {"brokerage_flat": 0, "brokerage_pct": 0}),
                         charges(100000, 101000, {"brokerage_flat": 0, "brokerage_pct": 0})["total"])


if __name__ == "__main__":
    unittest.main()
