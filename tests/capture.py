#!/usr/bin/env python3
"""Snapshot live endpoints into test fixtures. Standard library only.

    python3 tests/capture.py <skill> --day YYYY-MM-DD [--out DIR] [--pause 0.3]

Each skill registers a list of (url_template, fixture_name) pairs in REGISTRY. A template may hold:

    {day}      the --day
    {p1} {p2}  unix seconds of 00:00 IST on --day and on the day after (Yahoo period1 / period2)
    {symbols}  one batched request for the skill's symbols, 20 at a time (Yahoo spark); the answers are merged
    {sym}      one request per symbol; the answers are bundled into one file {"SYMBOL": <answer>}

Fixtures land in tests/fixtures/<skill>/ (or --out). Files over 200 KB are gzipped. A skill may also
register a "symbols" function (what to request) and an "expected" function (what the tool says about the
fixtures it was just given, written to expected.json so the smoke test can assert against it).
Requests are paced at 0.25 s or more and send the same User-Agent as the tools.
"""

import argparse
import csv
import datetime
import gzip
import io
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = "Mozilla/5.0 (nse-day-trader-skill)"
MIN_PAUSE = 0.25
GZIP_OVER = 200 * 1024
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SPARK_MAX = 20

YAHOO = "https://query2.finance.yahoo.com"
NSE_EQUITY_L = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_SEC_LIST = "https://nsearchives.nseindia.com/content/equities/sec_list.csv"
NSE_ANNOUNCEMENTS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
GOOGLE_RELIANCE = ("https://news.google.com/rss/search?q=%22Reliance%20Industries%22%20share"
                   "&hl=en-IN&gl=IN&ceid=IN:en")
NEWS_KEEP = {"google-RELIANCE.xml": (15, None), "Online_announcements.xml": (30, "<title>UCO Bank</title>"),
             "Financial_Results.xml": (25, None)}
NSE_FINANCIAL_RESULTS = "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml"
FUNDAMENTALS = ("https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{sym}"
                "?type=quarterlyTotalRevenue,quarterlyGrossProfit,quarterlyOperatingIncome,quarterlyNetIncome,"
                "quarterlyDilutedEPS&period1=1640995200&period2={p2}")
NSE_BULK = "https://archives.nseindia.com/content/equities/bulk.csv"
NSE_BLOCK = "https://archives.nseindia.com/content/equities/block.csv"
NSE_INSIDER = "https://nsearchives.nseindia.com/content/RSS/Insider_Trading.xml"
NSE_HOLIDAY_MASTER = "https://www.nseindia.com/api/holiday-master?type=trading"

# Liquid names kept in the trimmed EQUITY_L.csv (plus M&M, BAJAJ-AUTO and two BE rows, see trim_equity_l).
SCANNER_KEEP = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "BHARTIARTL", "ITC", "LT", "AXISBANK",
    "KOTAKBANK", "HINDUNILVR", "M&M", "BAJAJ-AUTO", "MARUTI", "TATAMOTORS", "TATASTEEL", "SUNPHARMA",
    "NTPC", "POWERGRID", "ONGC", "COALINDIA", "WIPRO", "HCLTECH", "ADANIENT", "ADANIPORTS", "JSWSTEEL",
    "TITAN", "ULTRACEMCO", "BAJFINANCE", "ASIANPAINT", "NESTLEIND", "HINDALCO", "GRASIM", "CIPLA",
    "DRREDDY", "EICHERMOT", "HEROMOTOCO", "VEDL", "IDEA",
]

# Rows kept in the trimmed sec_list.csv (price bands): F&O names with no fixed band, 20%, 10% and 5% EQ
# stocks, BE/BZ trade-to-trade rows and one SME row. A synthetic DEMO row (EQ, band 20) is appended for tests.
RISK_KEEP = [
    "RELIANCE", "TCS", "INFY", "SBIN", "M&M", "BAJAJ-AUTO", "IDEA", "VEDL", "J&KBANK", "20MICRONS", "3MINDIA",
    "ABDL", "ACCURACY", "21STCENMGM", "A2ZINFRA", "ABAN", "3IINFOLTD", "AARNAV", "AGSTRA", "AAKAAR",
]
DEMO_SEC_ROW = 'DEMO,EQ,DEMO SYNTHETIC TEST STOCK,20,"-"'

REGISTRY = {
    "nse-deals": {
        # bulk.csv and block.csv hold the CURRENT day only (capture on the day you want, after the close), and
        # the insider RSS feed; "extra" then fetches the PIT XBRL file behind the first RSS items (IT_*.xml).
        # If a live file no longer holds the rows the tests assert, hand-build it in the exact live format.
        "files": [
            (NSE_BULK, "bulk.csv"),
            (NSE_BLOCK, "block.csv"),
            (NSE_INSIDER, "Insider_Trading.xml"),
        ],
        "extra": "deals_pit_files",
    },
    "nse-scanner": {
        "files": [
            (NSE_EQUITY_L, "EQUITY_L.csv"),
            (YAHOO + "/v7/finance/spark?symbols={symbols}&range=5d&interval=5m", "spark-5m-{day}.json"),
            (YAHOO + "/v8/finance/chart/{sym}?period1={p1}&period2={p2}&interval=5m", "chart-5m-{day}.json"),
            (YAHOO + "/v7/finance/spark?symbols={symbols}&range=3mo&interval=1d", "daily-spark-{day}.json"),
        ],
    },
    "nse-risk": {
        # NSE's security list with each stock's series and price band (nse-risk circuit and series checks)
        "files": [
            (NSE_SEC_LIST, "sec_list.csv"),
        ],
    },
    "nse-entry": {
        # one liquid name, one session: a plain Yahoo chart JSON, the file entry.py --offline reads
        "symbols": ["RELIANCE"],
        "single": True,
        "files": [
            (YAHOO + "/v8/finance/chart/{sym}?period1={p1}&period2={p2}&interval=5m", "RELIANCE-5m-{day}.json"),
        ],
    },
    "nse-alerts": {
        # one Yahoo chart JSON per symbol (the files alerts.py --offline reads: yahoo-<SYMBOL>.json), and
        # NSE's holiday master trimmed to the CM list plus CBM (alerts.py calendar --refresh --offline reads it).
        # yahoo-ZZZZNOPE.json (Yahoo's 404 body for an unknown symbol) is kept by hand: a 404 raises here.
        "symbols": ["RELIANCE", "M&M", "TCS"],
        "per_symbol": True,
        "files": [
            (YAHOO + "/v8/finance/chart/{sym}?range=1d&interval=1d", "yahoo-{sym}.json"),
            (NSE_HOLIDAY_MASTER, "holiday-master.json"),
        ],
    },
    "nse-news": {
        # Google News India RSS for one company query, NSE's corporate announcements RSS (both trimmed, see
        # trim_items) and one Yahoo chart JSON (chart-RELIANCE.json, the move news.py reads)
        "symbols": ["RELIANCE"],
        "per_symbol": True,
        "files": [
            (GOOGLE_RELIANCE, "google-RELIANCE.xml"),
            (NSE_ANNOUNCEMENTS, "Online_announcements.xml"),
            (YAHOO + "/v8/finance/chart/{sym}?range=1d&interval=5m", "chart-{sym}.json"),
        ],
    },
    "nse-analyst": {
        # Yahoo fundamentals-timeseries per symbol (timeseries-<SYMBOL>.json; HDFCBANK is a bank with no gross
        # profit) and NSE's results-filed RSS (trimmed). universe.json (symbol -> company name, the file
        # results.py reads for the RSS match) is written by hand, see tests/fixtures/nse-analyst.
        "symbols": ["RELIANCE", "HDFCBANK"],
        "per_symbol": True,
        "files": [
            (FUNDAMENTALS, "timeseries-{sym}.json"),
            (NSE_FINANCIAL_RESULTS, "Financial_Results.xml"),
        ],
    },
    "nse-timemachine": {
        # 60 days of 5-minute bars for one liquid name, the file timemachine.py --offline reads
        # (gzipped, over 200 KB). plan-RELIANCE.json is built by hand from a real session in that data.
        "symbols": ["RELIANCE"],
        "single": True,
        "files": [
            (YAHOO + "/v8/finance/chart/{sym}?range=60d&interval=5m", "RELIANCE-60d5m.json"),
        ],
    },
}


# ---------------------------------------------------------------- network

_last = [0.0]


def get(url, pause):
    wait = max(pause, MIN_PAUSE) - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode("utf-8", "replace")
    finally:
        _last[0] = time.time()


def quote(sym):
    return urllib.parse.quote(sym + ".NS", safe="")


# ---------------------------------------------------------------- scanner helpers

def trim_equity_l(text):
    """EQUITY_L.csv -> the same file cut to the fixture rows, header and spacing untouched."""
    lines = text.replace("\r", "").split("\n")
    header, rows = lines[0], [ln for ln in lines[1:] if ln.strip()]
    cols = [h.strip().upper() for h in next(csv.reader([header]))]
    si, ei = cols.index("SYMBOL"), cols.index("SERIES")
    keep, be = {}, []
    for ln in rows:
        f = next(csv.reader([ln]))
        sym, series = f[si].strip(), f[ei].strip()
        if series == "EQ" and sym in SCANNER_KEEP:
            keep[sym] = ln
        elif series == "BE" and len(be) < 2:
            be.append(ln)
    missing = [s for s in SCANNER_KEEP if s not in keep]
    if missing:
        print("capture: not in today's EQUITY_L.csv (skipped): %s" % ", ".join(missing), file=sys.stderr)
    out = [header] + [keep[s] for s in SCANNER_KEEP if s in keep] + be
    return "\n".join(out) + "\n"


def trim_sec_list(text):
    """sec_list.csv -> the RISK_KEEP rows (first row per symbol) plus the synthetic DEMO row."""
    lines = text.replace("\r", "").split("\n")
    header, rows = lines[0], [ln for ln in lines[1:] if ln.strip()]
    keep = {}
    for ln in rows:
        sym = next(csv.reader([ln]))[0].strip()
        if sym in RISK_KEEP and sym not in keep:
            keep[sym] = ln
    missing = [s for s in RISK_KEEP if s not in keep]
    if missing:
        print("capture: not in today's sec_list.csv (skipped): %s" % ", ".join(missing), file=sys.stderr)
    return "\n".join([header] + [keep[s] for s in RISK_KEEP if s in keep] + [DEMO_SEC_ROW]) + "\n"


def trim_holiday_master(doc):
    """holiday-master?type=trading -> the CM list (what alerts.py reads) plus CBM, one other segment."""
    return {k: doc[k] for k in ("CM", "CBM") if k in doc}


def trim_items(text, limit, must=None):
    """RSS text -> the first `limit` <item> blocks (plus the first one holding `must`), the rest untouched."""
    import re
    items = list(re.finditer(r"<item>.*?</item>\s*", text, re.S))
    if not items:
        return text
    keep = items[:limit]
    if must and not any(must in m.group(0) for m in keep):
        extra = next((m for m in items if must in m.group(0)), None)
        if extra:
            keep = keep[:-1] + [extra]
        else:
            print("capture: no item with %s in today's feed" % must, file=sys.stderr)
    return text[:items[0].start()] + "".join(m.group(0) for m in keep) + text[items[-1].end():]


def eq_symbols(csv_text):
    rows = list(csv.reader(io.StringIO(csv_text)))
    cols = [h.strip().upper() for h in rows[0]]
    si, ei = cols.index("SYMBOL"), cols.index("SERIES")
    return [r[si].strip() for r in rows[1:] if r and r[ei].strip() == "EQ"]


def trim_to_day(doc, day, keep_before=False):
    """Cut every Yahoo answer in doc to bars on `day` (or up to it with keep_before)."""
    def cut(res):
        ts = res.get("timestamp") or []
        idx = []
        for i, t in enumerate(ts):
            d = datetime.datetime.fromtimestamp(t, IST).date()
            if d == day or (keep_before and d < day):
                idx.append(i)
        res["timestamp"] = [ts[i] for i in idx]
        for q in (res.get("indicators") or {}).get("quote") or []:
            for k, v in list(q.items()):
                if isinstance(v, list):
                    q[k] = [v[i] for i in idx if i < len(v)]
    if "spark" in doc:
        for item in doc["spark"].get("result") or []:
            for res in item.get("response") or []:
                cut(res)
    elif "chart" in doc:
        for res in doc["chart"].get("result") or []:
            cut(res)
    else:
        for v in doc.values():
            trim_to_day(v, day, keep_before)
    return doc


def scanner_expected(day, outdir):
    """Run the tool on the fixtures just written; record the ORB (10:15) and daily outcome."""
    tool = os.path.join(ROOT, "nse-scanner", "scan.py")
    bundle = os.path.join(outdir, "chart-5m-%s.json.gz" % day)
    if not os.path.exists(bundle):
        bundle = bundle[:-3]
    nxt = (day + datetime.timedelta(days=1)).isoformat()
    exp = {"day": day.isoformat(), "at": "10:15", "captured_at": datetime.datetime.now(IST).isoformat(timespec="seconds")}
    r = subprocess.run([sys.executable, tool, "orb", "--offline", bundle, "--now", "%s 10:15" % day,
                        "--json", "--no-write", "--quiet"], capture_output=True, text=True)
    doc = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {"symbols": [], "scanned": 0}
    exp["orb_hits"] = len(doc["symbols"])
    exp["orb_symbols"] = [s["symbol"] for s in doc["symbols"]]
    exp["orb_scanned"] = doc.get("scanned", 0)
    dfile = os.path.join(outdir, "daily-spark-%s.json" % day)
    if os.path.exists(dfile + ".gz"):
        dfile += ".gz"
    r = subprocess.run([sys.executable, tool, "daily", "--offline", dfile, "--now", "%s 08:00" % nxt,
                        "--json", "--no-write", "--quiet"], capture_output=True, text=True)
    doc = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {"symbols": [], "scanned": 0}
    exp["daily_hits"] = len(doc["symbols"])
    exp["daily_symbols"] = sorted(s["symbol"] for s in doc["symbols"])
    return exp


def entry_expected(day, outdir):
    """Run entry.py on the RELIANCE session just written; record what it says at 10:15 and at the close."""
    tool = os.path.join(ROOT, "nse-entry", "entry.py")
    chart = os.path.join(outdir, "RELIANCE-5m-%s.json" % day)
    exp = {"symbol": "RELIANCE", "day": day.isoformat(),
           "captured_at": datetime.datetime.now(IST).isoformat(timespec="seconds"), "checks": {}}
    for when in ("10:15", "15:30"):
        r = subprocess.run([sys.executable, tool, "check", "RELIANCE", "--offline", chart, "--now",
                            "%s %s" % (day, when), "--json"], capture_output=True, text=True)
        res = json.loads(r.stdout)
        t = res.get("trigger") or {}
        exp["or_high"] = res["opening_range"]["high"]
        exp["or_low"] = res["opening_range"]["low"]
        exp["checks"][when] = {"status": res["status"], "level": res["level"],
                               "trigger_time": t.get("time"), "trigger_price": t.get("price")}
    return exp


TM_SEED = 19


def timemachine_expected(day, outdir):
    """Run timemachine.py (seed 19) on the plan-RELIANCE.json fixture and the 60-day bars just written."""
    tool = os.path.join(ROOT, "nse-timemachine", "timemachine.py")
    plan = os.path.join(outdir, "plan-RELIANCE.json")
    chart = os.path.join(outdir, "RELIANCE-60d5m.json")
    if not os.path.exists(chart):
        chart += ".gz"
    if not os.path.exists(plan):
        print("capture: no plan-RELIANCE.json in %s yet: build it from the data, then re-run" % outdir, file=sys.stderr)
        return {"symbol": "RELIANCE", "day": day.isoformat()}
    with open(plan, encoding="utf-8") as fh:
        pdate = json.load(fh)["date"]
    r = subprocess.run([sys.executable, tool, "run", "--plan", plan, "--offline", chart, "--now", "%s 23:00" % pdate,
                        "--seed", str(TM_SEED), "--json"], capture_output=True, text=True)
    doc = json.loads(r.stdout)
    mc, h = doc["mc"], doc["history"]
    return {"symbol": "RELIANCE", "day": day.isoformat(),
            "captured_at": datetime.datetime.now(IST).isoformat(timespec="seconds"),
            "plan_date": pdate, "now": "%s 23:00" % pdate, "seed": TM_SEED, "replays": mc["replays"],
            "pool_sessions": mc["pool_sessions"], "pool_from": mc["pool_from"], "pool_to": mc["pool_to"],
            "bars_left": mc["bars_left"], "until": mc["until"],
            "p_target": mc["p_target"], "p_stop": mc["p_stop"], "p_neither": mc["p_neither"],
            "p_green": mc["p_green"], "median_close": mc["median_close"], "p5_close": mc["p5_close"],
            "p95_close": mc["p95_close"], "avg_R": mc["avg_R"], "avg_r_net": mc["avg_r_net"],
            "history": {"sessions": h["sessions"], "triggered": h["triggered"], "target": h["target"],
                        "stop": h["stop"], "close": h["close"], "avg_R": h["avg_R"], "avg_r_net": h["avg_r_net"]},
            "excluded_sessions": doc["data"]["short_sessions_left_out"],
            "verdict": doc["verdict"]}


EXPECTED = {"nse-scanner": scanner_expected, "nse-entry": entry_expected, "nse-timemachine": timemachine_expected}


# ---------------------------------------------------------------- driver

def deals_pit_files(outdir, pause, count=3):
    """Fetch the XBRL file behind the first `count` distinct links of the saved Insider_Trading.xml."""
    import re
    with open(os.path.join(outdir, "Insider_Trading.xml"), encoding="utf-8") as fh:
        links = re.findall(r"<link>(https://[^<]*/IT_[^<]*\.xml)</link>", fh.read())
    done = []
    for link in list(dict.fromkeys(links))[:count]:
        done.append(write_fixture(outdir, link.rsplit("/", 1)[-1], get(link, pause)))
    return done


EXTRAS = {"deals_pit_files": deals_pit_files}


def write_fixture(outdir, name, text):
    path = os.path.join(outdir, name)
    data = text.encode("utf-8")
    if len(data) > GZIP_OVER:
        path += ".gz"
        data = gzip.compress(data, 9, mtime=0)
    with open(path, "wb") as fh:
        fh.write(data)
    return path, len(data)


def day_bounds(day):
    start = datetime.datetime(day.year, day.month, day.day, tzinfo=IST)
    return int(start.timestamp()), int((start + datetime.timedelta(days=1)).timestamp())


def capture(skill, day, outdir, pause):
    spec = REGISTRY[skill]
    os.makedirs(outdir, exist_ok=True)
    p1, p2 = day_bounds(day)
    symbols = list(spec.get("symbols", []))
    written = []
    for template, fname in spec["files"]:
        name = fname.replace("{sym}", "\0").format(day=day.isoformat())
        url = template.format(day=day.isoformat(), p1=p1, p2=p2, symbols="{symbols}", sym="{sym}")
        if "{symbols}" in url:
            merged = {"spark": {"result": [], "error": None}}
            for i in range(0, len(symbols), SPARK_MAX):
                batch = ",".join(quote(s) for s in symbols[i:i + SPARK_MAX])
                doc = json.loads(get(url.replace("{symbols}", batch), pause))
                merged["spark"]["result"].extend((doc.get("spark") or {}).get("result") or [])
            body = merged
        elif "{sym}" in url:
            body = {}
            for s in symbols:
                try:
                    body[s] = json.loads(get(url.replace("{sym}", quote(s)), pause))
                except Exception as e:  # a symbol Yahoo does not know: leave it out, say so
                    print("capture: %s: %s" % (s, e), file=sys.stderr)
            if spec.get("per_symbol"):
                for s, doc in body.items():
                    written.append(write_fixture(outdir, name.replace("\0", s), json.dumps(doc, separators=(",", ":"))))
                continue
            if spec.get("single") and len(body) == 1:
                body = next(iter(body.values()))
        else:
            text = get(url, pause)
            if name == "EQUITY_L.csv":
                text = trim_equity_l(text)
                symbols = eq_symbols(text)
            elif name == "sec_list.csv":
                text = trim_sec_list(text)
            elif name in NEWS_KEEP:
                text = trim_items(text, *NEWS_KEEP[name])
            elif name == "holiday-master.json":
                text = json.dumps(trim_holiday_master(json.loads(text)), separators=(",", ":"))
            written.append(write_fixture(outdir, name, text))
            continue
        if "-5m-" in name:
            body = trim_to_day(body, day)
        elif "daily-" in name:
            body = trim_to_day(body, day, keep_before=True)
        written.append(write_fixture(outdir, name, json.dumps(body, separators=(",", ":"))))
    if spec.get("extra"):
        written.extend(EXTRAS[spec["extra"]](outdir, pause))
    fn = EXPECTED.get(skill)
    if fn:
        exp = fn(day, outdir)
        written.append(write_fixture(outdir, "expected.json", json.dumps(exp, indent=2) + "\n"))
        print("expected.json: %s" % json.dumps(exp))
    for path, size in written:
        print("wrote %s (%s bytes)" % (os.path.relpath(path, ROOT), "{:,}".format(size)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Snapshot live endpoints into tests/fixtures/<skill>/.")
    ap.add_argument("skill", choices=sorted(REGISTRY))
    ap.add_argument("--day", required=True, help="trading day to capture, YYYY-MM-DD (IST)")
    ap.add_argument("--out", help="fixture folder (default tests/fixtures/<skill>)")
    ap.add_argument("--pause", type=float, default=0.3, help="seconds between requests (min %.2f)" % MIN_PAUSE)
    a = ap.parse_args(argv)
    try:
        day = datetime.date.fromisoformat(a.day)
    except ValueError:
        print("capture: --day wants YYYY-MM-DD", file=sys.stderr)
        return 1
    capture(a.skill, day, a.out or os.path.join(ROOT, "tests", "fixtures", a.skill), a.pause)
    return 0


if __name__ == "__main__":
    sys.exit(main())
