import datetime as dt
import json
import os
import unittest
from decimal import Decimal

from _load import FIX, load

ob = load("nse-orderbook", "orderbook")
TXT = os.path.join(FIX, "nse-orderbook", "txt")
CR = Decimal(10) ** 7

# real filings, value read by hand from each text (crore, basis, flags)
EXPECTED = {
    "106046301": ("4099.70", "text", ["l1"]),
    "106050249": ("33.53", "text", ["loi"]),
    "106050809": ("460.30", "text", ["fx"]),
    "106516933": ("1020.00", "text", []),
    "106517159": ("0.29", "text", []),
    "106519251": ("1350.00", "band", ["loi", "range"]),
    "106814753": ("13.04", "text", []),
    "106815247": ("2225.00", "text", []),
    "106817424": ("228.00", "text", ["loi"]),
    "106817529": ("6.75", "text", []),
    "106819245": (None, "none", []),
    "106819915": ("116.23", "text", ["gst"]),
    "106820384": ("126.69", "text", ["gst"]),
    "106821513": ("15000.00", "band", ["range"]),
    "106823688": (None, "none", ["loi"]),
    "106826071": ("2.66", "text", ["gst"]),
}


def cr(v):
    return None if v is None else str((v / CR).quantize(Decimal("0.01")))


class Values(unittest.TestCase):
    def test_real_filings(self):
        for seq, (val, basis, flags) in EXPECTED.items():
            with open(os.path.join(TXT, seq + ".txt"), encoding="utf-8", errors="replace") as fh:
                got = ob.parse_value(fh.read())
            self.assertEqual((cr(got["value_inr"]), got["basis"], got["flags"]), (val, basis, flags), seq)

    def test_formats(self):
        self.assertEqual(ob.parse_value("order worth Rs. 3,13,57,320")["value_inr"], Decimal("31357320"))
        self.assertEqual(ob.parse_value("new orders of ₹2,225 Crores")["value_inr"], Decimal("22250000000"))
        self.assertEqual(ob.parse_value("workorder worth Rs.6.75 crores")["value_inr"], Decimal("67500000"))
        band = ob.parse_value("Large Order: Rs 100-500 Cr received")
        self.assertEqual((band["value_inr"], band["basis"], band["flags"]), (Decimal("3000000000"), "band", ["range"]))
        gst = ob.parse_value("order of Rs 118 crore (including GST) received")
        self.assertEqual((gst["value_inr"], gst["flags"]), (Decimal("1000000000"), ["gst"]))
        fx = ob.parse_value("export order for USD 12 million")
        self.assertEqual((fx["value_inr"], fx["flags"]), (Decimal("1008000000"), ["fx"]))
        self.assertEqual(ob.parse_value("Kindly take the above on record.")["basis"], "none")

    def test_turnover_and_legend_are_not_the_order(self):
        t = ("The company's turnover was Rs 5,000 crore last year. It has received an order worth Rs 40 crore. "
             "EMD of Rs 900 crore was paid.")
        self.assertEqual(ob.parse_value(t)["value_inr"], Decimal("400000000"))
        self.assertIsNone(ob.parse_value("Order size: Upto INR 50 crore | Above INR 100 crore")["value_inr"])


class Rows(unittest.TestCase):
    def test_categories_and_dedupe(self):
        raw = [{"seq_id": "1", "symbol": "abc", "desc": "Bagging/Receiving of orders/contracts", "an_dt": "15-Nov-2024 13:31:09",
                "attchmntFile": "https://x/a.pdf", "sort_date": "2024-11-15 13:31:09", "sm_name": "A Ltd"},
               {"seq_id": "1", "symbol": "ABC", "desc": "Bagging/Receiving of orders/contracts",
                "sort_date": "2024-11-15 13:31:09"},
               {"seq_id": "2", "symbol": "ABC", "desc": "Bagging/Receiving of orders/contracts", "attchmntFile": "https://y/A.PDF",
                "sort_date": "2024-11-16"},
               {"seq_id": "3", "symbol": "XYZ", "desc": "Rescission/termination(s)", "an_dt": "16-Nov-2024 10:00:00",
                "sort_date": "2024-11-16"},
               {"seq_id": "4", "symbol": "XYZ", "desc": "Action(s) taken or orders passed", "sort_date": "2024-11-16"}]
        rows = ob.order_rows(raw)
        self.assertEqual([(r["seq_id"], r["kind"]) for r in rows], [("1", "order"), ("3", "termination")])
        self.assertEqual(rows[0]["at"], "2024-11-15T13:31:09+05:30")
        self.assertEqual(rows[0]["symbol"], "ABC")


def order(seq, day, crore, kind="order"):
    return {"seq_id": seq, "symbol": "S", "kind": kind, "at": "%sT10:00:00+05:30" % day,
            "value_inr": str(Decimal(crore) * CR) if crore is not None else None, "basis": "text", "flags": []}


def bars(start, n, price=100, volume=2000000):
    return [{"date": start + dt.timedelta(days=i), "open": Decimal(price), "close": Decimal(price),
             "volume": volume} for i in range(n)]


ANNUAL = [(dt.date(2024, 3, 31), Decimal(800) * CR), (dt.date(2025, 3, 31), Decimal(1000) * CR)]
CFG = json.loads(json.dumps(ob.DEFAULTS))


class Screen(unittest.TestCase):
    D = dt.date(2025, 7, 31)

    def run_screen(self, orders, annual=ANNUAL, px=None, quarterly=()):
        data = lambda s: (px if px is not None else bars(dt.date(2025, 1, 1), 240), annual, list(quarterly))
        return ob.screen(self.D, {"S": orders}, data, CFG)

    def test_passes_and_scores(self):
        picks, rej = self.run_screen([order("1", "2025-07-01", 150), order("0", "2025-02-01", 60)])
        self.assertEqual([p["symbol"] for p in picks], ["S"])
        p = picks[0]
        self.assertEqual(p["intensity"], Decimal("0.15"))
        self.assertEqual(p["accel"], Decimal(150) / (Decimal(60) / 3))   # 7.5x
        self.assertEqual(p["score"], Decimal("0.15") * 3)               # accel capped at 3
        self.assertEqual(p["rev_growth"], Decimal("0.25"))

    def test_revenue_must_grow(self):
        flat = [(dt.date(2024, 3, 31), Decimal(980) * CR), (dt.date(2025, 3, 31), Decimal(1000) * CR)]
        picks, rej = self.run_screen([order("1", "2025-07-01", 200)], annual=flat)
        self.assertEqual(picks, [])
        self.assertEqual(rej, {"rev_not_growing": ["S"]})

    def test_no_look_ahead(self):
        later = [order("1", "2025-07-01", 20), order("9", "2025-08-01", 5000)]   # the big one is after D
        self.assertEqual(self.run_screen(later)[1], {"low_intensity": ["S"]})
        future_fy = ANNUAL + [(dt.date(2025, 6, 30), Decimal(1) * CR)]            # FY end + 60 days > D
        self.assertEqual(ob.revenue_at(future_fy, self.D)[0], Decimal(1000) * CR)
        self.assertEqual(ob.revenue_at(ANNUAL, dt.date(2025, 5, 20))[0], Decimal(800) * CR)

    def test_filters(self):
        self.assertEqual(self.run_screen([order("1", "2025-07-01", None)])[1], {"no_value": ["S"]})
        self.assertEqual(self.run_screen([order("1", "2025-07-01", 200), order("2", "2025-07-05", 70, "termination")])[1],
                         {"terminations": ["S"]})
        self.assertEqual(self.run_screen([order("1", "2025-07-01", 200)], px=bars(dt.date(2025, 1, 1), 240, volume=1000))[1],
                         {"illiquid": ["S"]})
        self.assertEqual(self.run_screen([order("1", "2025-07-01", 200)], px=[])[1], {"no_price_data": ["S"]})
        self.assertEqual(self.run_screen([order("1", "2025-07-01", 200), order("0", "2025-03-01", 600)])[1],
                         {"no_accel": ["S"]})


class Backtest(unittest.TestCase):
    def test_delivery_charges(self):
        self.assertEqual(ob.delivery_charges(100000, 110000), Decimal("248.79"))
        self.assertEqual(ob.delivery_charges(100000, 0, 0), Decimal("118.74"))

    def test_round_trip_and_hold(self):
        start = dt.date(2025, 6, 1)
        cal = [start + dt.timedelta(days=i) for i in range(250)]
        bench = [{"date": d, "close": Decimal(100)} for d in cal]
        px = [{"date": d, "open": Decimal(100 + i), "close": Decimal(100 + i), "volume": 2000000}
              for i, d in enumerate(cal)]
        data = lambda s: (px, ANNUAL, [])
        cfg = json.loads(json.dumps(CFG))
        cfg["capital"] = 100000
        res = ob.backtest(dt.date(2025, 6, 1), dt.date(2026, 1, 31), {"S": [order("1", "2025-06-10", 200)]}, data,
                          cfg, bench)
        self.assertEqual(res["months"][0]["picks"], ["S"])
        buys = [t for t in res["trades"] if t["side"] == "BUY"]
        sells = [t for t in res["trades"] if t["side"] == "SELL"]
        self.assertEqual(buys[0]["date"], "2025-07-01")          # next open after the 30 June signal
        self.assertEqual(res["months"][2]["picks"], ["S"])        # still in the 90-day window on 31 Aug
        self.assertEqual(res["months"][3]["picks"], [])           # out of it by 30 Sep
        self.assertEqual(sells[-1]["date"], "2025-12-01")        # 3 months after the last pick, sold
        self.assertGreater(res["stats"]["final"], Decimal(100000))
        self.assertGreater(res["stats"]["charges"], 0)
        self.assertEqual(res["stats"]["bench_total"], 0)


if __name__ == "__main__":
    unittest.main()
