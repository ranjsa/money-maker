import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, ROOT, FIX

paper = load("nse-broker", "paper")
check = load("nse-risk", "check")

ORDERS_PATH = os.path.join(FIX, "nse-risk", "orders-demo.json")
BARS_PATH = os.path.join(FIX, "nse-broker", "DEMO-5m-paper.json")
GROUP = "DEMO-2026-10-27-1"
QTY = 73
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def ts(hhmm):
    h, m = int(hhmm[:2]), int(hhmm[3:])
    return int(datetime.datetime(2026, 10, 27, h, m, tzinfo=IST).timestamp())


def net(entry, exit_):
    gross = (Decimal(str(exit_)) - Decimal(str(entry))) * QTY
    return gross - paper.round_trip(QTY, Decimal(str(entry)), Decimal(str(exit_)))


class PaperRun(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.bars = os.path.join(self.d, "bars")
        os.makedirs(self.bars)
        with open(BARS_PATH, encoding="utf-8") as fh:
            self.chart = json.load(fh)
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.state = os.path.join(self.d, "state", "broker.json")
        self.replay = os.path.join(self.d, "state", "paper-replay.json")
        self.cfg = os.path.join(self.d, "config.json")
        self.saved_open = paper._open_url
        # the real clock for a recorded yes: the end of the fixture's session (Gap fix: the yes must be today)
        self.saved_now = paper.real_now if hasattr(paper, "real_now") else None
        paper.real_now = lambda: datetime.datetime(2026, 10, 27, 10, 0, tzinfo=datetime.timezone.utc)

        def no_network(*a, **k):
            raise AssertionError("paper.py touched the network with --offline")
        paper._open_url = no_network

    def tearDown(self):
        paper._open_url = self.saved_open
        if self.saved_now is None:
            del paper.real_now
        else:
            paper.real_now = self.saved_now
        shutil.rmtree(self.d)

    def set_bar(self, hhmm, o, h, l, c):
        r = self.chart["chart"]["result"][0]
        i = r["timestamp"].index(ts(hhmm))
        q = r["indicators"]["quote"][0]
        q["open"][i], q["high"][i], q["low"][i], q["close"][i] = o, h, l, c

    def cut_after(self, hhmm):
        r = self.chart["chart"]["result"][0]
        i = r["timestamp"].index(ts(hhmm)) + 1
        r["timestamp"] = r["timestamp"][:i]
        for k, v in r["indicators"]["quote"][0].items():
            r["indicators"]["quote"][0][k] = v[:i]

    def go(self, *extra, approved="2026-10-27 09:35"):
        with open(os.path.join(self.bars, "DEMO.json"), "w", encoding="utf-8") as fh:
            json.dump(self.chart, fh)
        argv = ["run", "--group", GROUP, "--orders", self.orders, "--state", self.state, "--config", self.cfg,
                "--offline", self.bars] + list(extra)
        if approved:
            argv += ["--approved-at", approved]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = paper.main(argv)
        return code, out.getvalue(), err.getvalue()

    def status(self, path=None):
        """Open positions in the replay file (the --approved-at runs) unless a path is given."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = paper.main(["status", "--state", path or self.replay])
        return code, out.getvalue()

    def records(self, path=None):
        """--approved-at runs are replays: they write nse/state/paper-replay.json, never broker.json."""
        with open(path or self.replay, encoding="utf-8") as fh:
            return json.load(fh)["orders"]

    def exit_rec(self, path=None):
        return [r for r in self.records(path) if r.get("status") == "filled" and r.get("leg") != "entry"][-1]

    def approve_at(self, at, broker="paper"):
        """A yes as ticket.py approve records it in broker.json."""
        fp = check.fingerprint(json.load(open(ORDERS_PATH))["orders"][0])
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump({"orders": [{"at": at, "date": at[:10], "status": "approved", "group": GROUP,
                                   "fingerprint": fp, "broker": broker, "max_loss": 788.66}]}, fh)

    def write_cfg(self, **over):
        with open(os.path.join(ROOT, "nse-budget", "config.example.json"), encoding="utf-8") as fh:
            c = json.load(fh)
        c.update(over)
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump(c, fh)

    def test_stop_hit_at_the_trigger(self):
        self.set_bar("10:05", 1009.0, 1010.0, 1004.0, 1006.0)
        code, text, err = self.go()
        self.assertEqual(code, 0, err)
        want = net("1015.00", "1005.20")
        self.assertEqual(want, Decimal(-QTY) * Decimal("9.80") - paper.round_trip(QTY, Decimal("1015.00"),
                                                                                  Decimal("1005.20")))
        ex = self.exit_rec()
        self.assertEqual((ex["leg"], ex["price"], ex["time"]), ("stop", 1005.2, "10:05"))
        self.assertEqual(ex["net_pnl"], float(want))
        self.assertIn("stop", text)
        self.assertIn("1,005.20", text)
        self.assertIn(paper.inr(want), text)
        recs = self.records()
        ent = [r for r in recs if r.get("leg") == "entry"][0]
        self.assertEqual((ent["price"], ent["time"], ent["qty"]), (1015.0, "09:35", QTY))
        placed = [r for r in recs if r.get("status") == "placed"]
        self.assertEqual(len(placed), 1)
        self.assertEqual((placed[0]["kind"], placed[0]["max_loss"], placed[0]["legs"], placed[0]["broker"]),
                         ("entry", 788.66, 3, "paper"))
        self.assertEqual(placed[0]["fingerprint"], check.fingerprint(json.load(open(ORDERS_PATH))["orders"][0]))
        # a replay is never in broker.json, so nse-risk never counts it
        self.assertFalse(os.path.exists(self.state))
        self.assertIn("REPLAY", text)

    def test_a_recorded_yes_plays_into_broker_json_and_risk_counts_it(self):
        self.approve_at("2026-10-27T09:35:00+05:30")
        self.set_bar("10:05", 1009.0, 1010.0, 1004.0, 1006.0)
        code, text, err = self.go(approved=None)
        self.assertEqual(code, 0, err)
        self.assertNotIn("REPLAY", text)
        self.assertFalse(os.path.exists(self.replay))
        recs = self.records(self.state)
        self.assertEqual(self.exit_rec(self.state)["net_pnl"], float(net("1015.00", "1005.20")))
        placed = [r for r in recs if r.get("status") == "placed"]
        self.assertEqual((placed[0]["kind"], placed[0]["max_loss"], placed[0]["broker"]), ("entry", 788.66, "paper"))
        # nse-risk counts the paper trade like a live one
        res = check.check([], {"budget": 100000}, state={"orders": recs}, today="2026-10-27",
                          now=check.parse_now("2026-10-27 11:00"))
        self.assertEqual(res["today"], {"orders_placed": 3, "entries_placed": 1, "loss_at_risk": 788.66})

    def test_the_logged_max_loss_is_measured_from_the_entry_fill(self):
        # I6: the entry filled at 1017.00, not the plan's 1015.00: the loss at the stop is from 1017.00
        self.approve_at("2026-10-27T09:37:00+05:30")
        self.set_bar("09:40", 1017.0, 1019.0, 1014.0, 1018.0)
        code, text, err = self.go(approved=None)
        self.assertEqual(code, 0, err)
        placed = [r for r in self.records(self.state) if r.get("status") == "placed"][0]
        want = Decimal(QTY) * (Decimal("1017.00") - Decimal("1005.20")) + paper.round_trip(
            QTY, Decimal("1017.00"), Decimal("1005.20"))
        self.assertEqual(placed["max_loss"], float(want))
        self.assertGreater(placed["max_loss"], 788.66)

    def test_a_replay_prints_replay_and_never_touches_broker_json(self):
        self.approve_at("2026-10-27T09:35:00+05:30")
        with open(self.state, encoding="utf-8") as fh:
            before = fh.read()
        code, text, err = self.go(approved="2026-10-27 10:00")
        self.assertEqual(code, 0, err)
        self.assertTrue(text.startswith("REPLAY"), text)
        with open(self.state, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(self.records()[-1]["status"], "filled")
        self.assertTrue(all(r.get("replay") for r in self.records()))

    def test_no_paper_entry_outside_the_entry_window(self):
        # I2
        for when, want in (("2026-10-27 14:50", "no new entries after 14:45"),
                           ("2026-10-27 09:20", "no new entries before 09:30")):
            code, _, err = self.go(approved=when)
            self.assertEqual(code, 1, when)
            self.assertIn(want, err, when)
            self.assertFalse(os.path.exists(self.replay))
        self.approve_at("2026-10-27T14:50:00+05:30")
        code, _, err = self.go(approved=None)
        self.assertEqual(code, 1)
        self.assertIn("no new entries after 14:45", err)
        self.assertEqual(len(self.records(self.state)), 1)

    def test_a_limit_entry_never_fills_after_the_window(self):
        with open(self.orders, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["orders"][0]["legs"][0].update(order_type="LIMIT", price=990.0)
        with open(self.orders, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        self.set_bar("14:50", 995.0, 996.0, 985.0, 990.0)
        code, text, err = self.go()
        self.assertEqual(code, 0, err)
        self.assertNotIn("entry       BUY", text)
        self.assertIn("entry window", text)
        self.assertFalse([r for r in self.records() if r.get("status") == "filled"])

    def test_broker_and_paper_that_disagree_are_a_config_error(self):
        # I5
        for broker, flag in (("kite", True), ("paper", False)):
            self.write_cfg(broker=broker, paper=flag)
            code, _, err = self.go()
            self.assertEqual(code, 1, broker)
            self.assertIn('"broker"', err)
            self.assertIn('"paper"', err)

    def test_a_live_config_never_plays_a_recorded_yes_on_paper(self):
        self.write_cfg(broker="kite", paper=False)
        self.approve_at("2026-10-27T09:35:00+05:30", broker="kite")
        code, _, err = self.go(approved=None)
        self.assertEqual(code, 1)
        self.assertIn("kite", err)
        self.assertEqual(len(self.records(self.state)), 1)

    def test_a_gap_below_the_trigger_fills_at_the_open(self):
        self.set_bar("10:05", 1003.0, 1006.0, 1001.0, 1004.0)
        code, text, err = self.go()
        self.assertEqual(code, 0, err)
        ex = self.exit_rec()
        self.assertEqual((ex["leg"], ex["price"]), ("stop", 1003.0))
        self.assertEqual(ex["net_pnl"], float(net("1015.00", "1003.00")))
        self.assertIn("gap", text)

    def test_target_hit_at_the_limit(self):
        self.set_bar("11:00", 1030.0, 1036.0, 1029.0, 1035.0)
        code, text, err = self.go()
        self.assertEqual(code, 0, err)
        ex = self.exit_rec()
        self.assertEqual((ex["leg"], ex["price"], ex["time"]), ("target", 1034.6, "11:00"))
        self.assertEqual(ex["net_pnl"], float(net("1015.00", "1034.60")))

    def test_a_gap_above_the_target_fills_at_the_open(self):
        self.set_bar("11:00", 1040.0, 1042.0, 1038.0, 1039.0)
        self.go()
        self.assertEqual(self.exit_rec()["price"], 1040.0)

    def test_both_in_one_bar_assumes_the_stop(self):
        self.set_bar("10:30", 1015.0, 1036.0, 1004.0, 1020.0)
        code, text, err = self.go()
        self.assertEqual(code, 0, err)
        ex = self.exit_rec()
        self.assertEqual((ex["leg"], ex["price"]), ("stop", 1005.2))
        self.assertIn("both", text)

    def test_no_hit_squares_off_at_the_flat_by_bar_close(self):
        code, text, err = self.go()
        self.assertEqual(code, 0, err)
        ex = self.exit_rec()
        self.assertEqual((ex["leg"], ex["price"], ex["time"]), ("square-off", 1021.4, "15:10"))
        self.assertEqual(ex["net_pnl"], float(net("1015.00", "1021.40")))
        self.assertIn("15:10", text)
        self.assertEqual(self.status(), (0, "No open paper positions.\n"))

    def test_entry_waits_for_the_first_bar_at_or_after_the_approval(self):
        self.set_bar("09:40", 1017.0, 1019.0, 1014.0, 1018.0)
        self.go(approved="2026-10-27 09:37")
        ent = [r for r in self.records() if r.get("leg") == "entry"][0]
        self.assertEqual((ent["price"], ent["time"]), (1017.0, "09:40"))

    def test_bars_before_the_entry_never_count(self):
        # the 09:20 bar's low 1003.00 is under the stop, but it is before the approval
        code, text, err = self.go()
        self.assertEqual(self.exit_rec()["leg"], "square-off")

    def test_an_open_position_when_the_data_runs_out(self):
        self.cut_after("11:00")
        code, text, err = self.go()
        self.assertEqual(code, 0, err)
        self.assertIn("still open", text)
        code, out = self.status()
        self.assertEqual(code, 0)
        self.assertIn("DEMO", out)
        self.assertIn("73", out)
        self.assertIn("1,005.20", out)
        # later in the day: the same command picks it up, no second entry
        with open(BARS_PATH, encoding="utf-8") as fh:
            self.chart = json.load(fh)
        self.set_bar("13:00", 1010.0, 1011.0, 1004.0, 1006.0)
        code, text, err = self.go()
        self.assertEqual(code, 0, err)
        recs = self.records()
        self.assertEqual(len([r for r in recs if r.get("leg") == "entry"]), 1)
        self.assertEqual(len([r for r in recs if r.get("status") == "placed"]), 1)
        self.assertEqual(self.exit_rec()["price"], 1005.2)
        self.assertEqual(self.status()[1], "No open paper positions.\n")

    def test_a_closed_group_is_not_run_twice(self):
        self.go()
        code, _, err = self.go()
        self.assertEqual(code, 1)
        self.assertIn("already", err)

    def test_without_a_yes_it_refuses(self):
        code, _, err = self.go(approved=None)
        self.assertEqual(code, 1)
        self.assertIn("ticket.py approve", err)

    def test_the_approval_time_comes_from_the_yes(self):
        fp = check.fingerprint(json.load(open(ORDERS_PATH))["orders"][0])
        os.makedirs(os.path.dirname(self.state))
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump({"orders": [{"at": "2026-10-27T09:36:10+05:30", "date": "2026-10-27", "status": "approved",
                                   "group": GROUP, "fingerprint": fp, "broker": "paper"}]}, fh)
        self.set_bar("09:40", 1017.0, 1019.0, 1014.0, 1018.0)
        code, _, err = self.go(approved=None)
        self.assertEqual(code, 0, err)
        ent = [r for r in self.records(self.state) if r.get("leg") == "entry"][0]
        self.assertEqual((ent["price"], ent["time"]), (1017.0, "09:40"))

    def test_approval_after_flat_by_is_refused(self):
        code, _, err = self.go(approved="2026-10-27 15:12")
        self.assertEqual(code, 1)
        self.assertIn("flat_by", err)

    # ------------------------------------------------ small items
    def test_a_replay_says_it_recorded_in_the_replay_file(self):
        code, text, err = self.go()
        self.assertEqual(code, 0, err)
        last = text.strip().splitlines()[-1]
        self.assertIn("Recorded in %s." % self.replay, last)
        self.assertNotIn(self.state, last)

    def test_a_replay_state_that_is_broker_json_is_refused(self):
        self.approve_at("2026-10-27T09:35:00+05:30")
        with open(self.state, encoding="utf-8") as fh:
            before = fh.read()
        default = os.path.join("nse", "state", "broker.json")
        for target in (self.state, os.path.join(self.d, "state", ".", "broker.json"), default):
            code, _, err = self.go("--replay-state", target)
            self.assertEqual(code, 1, target)
            self.assertIn("--replay-state", err, target)
            with open(self.state, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), before, target)
        self.assertFalse(os.path.exists(default))

    def test_a_recorded_yes_from_another_day_is_refused(self):
        self.approve_at("2026-10-26T09:35:00+05:30")
        code, _, err = self.go(approved=None)
        self.assertEqual(code, 1)
        self.assertIn("2026-10-26", err)
        self.assertIn("today", err)
        self.assertEqual(len(self.records(self.state)), 1)

    # ------------------------------------------------ residual fix 1
    def add_record(self, status, at):
        with open(self.state, encoding="utf-8") as fh:
            st = json.load(fh)
        r = dict(st["orders"][0])
        r.update(status=status, at=at, date=at[:10])
        st["orders"].append(r)
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump(st, fh)

    def test_a_yes_declined_later_is_refused(self):
        for later in ("declined", "denied", "failed"):
            self.approve_at("2026-10-27T09:35:00+05:30")
            self.add_record(later, "2026-10-27T09:36:00+05:30")
            with open(self.state, encoding="utf-8") as fh:
                before = fh.read()
            code, _, err = self.go(approved=None)
            self.assertEqual(code, 1, later)
            self.assertIn(later, err)
            with open(self.state, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), before, later)

    def test_a_yes_given_again_after_a_decline_runs(self):
        self.approve_at("2026-10-27T09:35:00+05:30")
        self.add_record("declined", "2026-10-27T09:36:00+05:30")
        self.add_record("approved", "2026-10-27T09:37:00+05:30")
        self.set_bar("10:05", 1009.0, 1010.0, 1004.0, 1006.0)
        code, _, err = self.go(approved=None)
        self.assertEqual(code, 0, err)
        self.assertEqual([r["status"] for r in self.records(self.state)][-3:], ["placed", "filled", "filled"])

    def test_a_decline_after_the_entry_filled_still_plays_the_exit(self):
        # round 2: the position is open on paper; a later "no" cannot strand it without its exit
        self.approve_at("2026-10-27T09:35:00+05:30")
        self.cut_after("11:00")
        code, text, err = self.go(approved=None)
        self.assertEqual(code, 0, err)
        self.assertIn("still open", text)
        self.add_record("declined", "2026-10-27T11:05:00+05:30")
        with open(BARS_PATH, encoding="utf-8") as fh:
            self.chart = json.load(fh)
        self.set_bar("13:00", 1010.0, 1011.0, 1004.0, 1006.0)
        code, text, err = self.go(approved=None)
        self.assertEqual(code, 0, err)
        recs = self.records(self.state)
        self.assertEqual(len([r for r in recs if r.get("leg") == "entry"]), 1)
        self.assertEqual(self.exit_rec(self.state)["price"], 1005.2)

    def test_every_paper_record_carries_the_real_clock(self):
        self.approve_at("2026-10-27T09:35:00+05:30")
        self.set_bar("10:05", 1009.0, 1010.0, 1004.0, 1006.0)
        self.assertEqual(self.go(approved=None)[0], 0)
        made = self.records(self.state)[1:]
        self.assertEqual(len(made), 3)
        want = paper.to_ist(paper.real_now()).isoformat()
        self.assertEqual([r.get("recorded_at") for r in made], [want] * 3)
        self.assertNotEqual(made[0]["at"], want)          # "at" stays the bar time

    def test_offline_never_touches_the_network(self):
        # setUp replaced the fetcher with one that raises; a full run must still work
        code, _, err = self.go()
        self.assertEqual(code, 0, err)
        self.assertNotIn("network", err)


if __name__ == "__main__":
    unittest.main()
