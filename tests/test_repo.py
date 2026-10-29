import json
import os
import re
import subprocess
import sys
import unittest

from _load import ROOT

# the NSE port: the twelve folders the repo card shows
SKILLS = ["nse-scanner", "nse-news", "nse-entry", "nse-exit", "nse-timemachine",
          "nse-risk", "nse-budget", "nse-broker", "nse-alerts", "nse-analyst",
          "nse-deals", "nse-orderbook"]
ABOUT = ("12 Claude skills for trading on NSE: scan every EQ chart, track exchange filings, order wins and news, "
         "time every entry and exit, and replay every outcome before you risk a rupee.")
# skills on the way to an order carry the safety block
ORDER_PATH = ["nse-scanner", "nse-entry", "nse-exit", "nse-timemachine", "nse-budget",
              "nse-deals", "nse-risk", "nse-broker", "nse-alerts"]
V1_ONLY = ["trader-copycat", "trader-screener", "trader-tax", "trader-rebalance"]


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def every_text_file():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".superpowers")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml")) or f == "LICENSE":
                yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_twelve_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT)
                       if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_root_matches_the_reel(self):
        root = sorted(d for d in os.listdir(ROOT) if d == ".claude-plugin" or not d.startswith("."))
        self.assertEqual(root, sorted([".claude-plugin", "LICENSE", "README.md", "docs", "tests"] + SKILLS))

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("yes", low, s)

    def test_plugin_manifest_points_at_the_root_skills(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as fh:
            plugin = json.load(fh)
        self.assertEqual(plugin["name"], "nse-day-trader-skill")
        self.assertEqual(plugin["skills"], "./")
        self.assertEqual(plugin["description"], ABOUT)
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as fh:
            market = json.load(fh)
        self.assertEqual(market["name"], "nse-day-trader-skill")
        self.assertEqual(market["plugins"][0]["name"], "nse-day-trader-skill")

    def test_readme_carries_the_reel_copy(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        self.assertTrue(readme.startswith("# The NSE Day Trader skill"))
        for line in (ABOUT,
                     "https://github.com/ranjsa/money-maker install skill",
                     "/plugin marketplace add ranjsa/money-maker",
                     "/plugin install nse-day-trader-skill@nse-day-trader-skill",
                     "## Fine print",
                     "71 % (7 in 10) of individual intraday traders", "PR 13/2024", "PR 37/2024",
                     "9 in 10 individual F&O traders (93 %)"):
            self.assertIn(line, readme)
        for s in SKILLS:
            self.assertIn("/" + s, readme, s)

    def test_common_blocks_identical(self):
        self.assertEqual(subprocess.call([sys.executable, "tests/sync_common.py", "--check"], cwd=ROOT), 0)

    def test_no_us_leftovers(self):  # outside tests/fixtures and the upstream credit (LICENSE, README credit lines)
        for path in every_text_file():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith(os.path.join("tests", "fixtures")) or rel == "LICENSE":
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            if rel == "README.md":  # only the lines that credit the upstream pack may name it
                text = "\n".join(l for l in text.splitlines() if "github.com/Jakeschincariol/" not in l)
            for word in ("New " + "York", "NY" + "SE", "ED" + "GAR", "Alp" + "aca", "Pel" + "osi", "trader" + "/"):
                self.assertNotIn(word, text, "%s: %s" % (rel, word))

    def test_no_em_dashes_and_no_personal_email(self):
        for path in every_text_file():
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            rel = os.path.relpath(path, ROOT)
            if not rel.startswith(os.path.join("tests", "fixtures")):  # fixtures are real data, kept as published
                self.assertNotIn("\u2014", text, rel)
            self.assertNotIn("jakeschincariol" + "@" + "gmail.com", text, rel)  # built, so this file passes too

    def test_no_v1_only_skill_is_left_behind(self):
        for path in every_text_file():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith("tests" + os.sep) or rel == "README.md":
                continue  # the README points v1 users to the v1 commit
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for old in V1_ONLY:
                self.assertNotIn(old, text, rel)

    def test_every_tool_runs_on_python_3_8(self):
        # no 3.9+ only modules or syntax in the tools (the README promises 3.8)
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                for bad in ("import zoneinfo", "from zoneinfo", ".removeprefix(", ".removesuffix(",
                            "match ", "list[", "dict[", "tuple["):
                    if bad == "match ":
                        self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", "%s/%s" % (s, f))
                    elif bad.endswith("["):
                        self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad), "%s/%s" % (s, f))
                    else:
                        self.assertNotIn(bad, src, "%s/%s" % (s, f))

    def test_day_trading_limits_ship_in_the_example_config(self):
        with open(os.path.join(ROOT, "nse-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        for k in ("max_loss_per_trade_inr", "max_daily_loss_inr", "max_order_inr", "min_order_inr",
                  "max_trades_per_day", "max_orders_per_day", "min_reward_risk", "require_stop",
                  "no_entries_before", "no_entries_after", "flat_by", "product", "exchange", "special_sessions",
                  "charges"):
            self.assertIn(k, cfg, k)
        self.assertFalse([k for k in cfg if k.endswith("_usd")])
        self.assertNotIn("pdt_guard", cfg)
        self.assertNotIn("sec_user_agent", cfg)
        self.assertEqual((cfg["product"], cfg["exchange"], cfg["flat_by"]), ("MIS", "NSE", "15:10"))
        self.assertEqual((cfg["no_entries_before"], cfg["no_entries_after"]), ("09:30", "14:45"))
        self.assertEqual((cfg["budget"], cfg["max_loss_per_trade_inr"], cfg["max_daily_loss_inr"]),
                         (100000, 1000, 3000))
        self.assertEqual((cfg["max_order_inr"], cfg["min_order_inr"]), (80000, 500))
        self.assertIs(cfg["require_stop"], True)
        for k in ("allow_leverage", "allow_short", "allow_fno", "allow_banded"):
            self.assertIs(cfg[k], False, k)
        self.assertEqual(cfg["special_sessions"], {})
        self.assertEqual(cfg["charges"], {})

    def test_paper_trading_is_the_default(self):
        with open(os.path.join(ROOT, "nse-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["paper"], True)
        self.assertEqual(cfg["broker"], "paper")


if __name__ == "__main__":
    unittest.main()
