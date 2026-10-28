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

# Liquid names kept in the trimmed EQUITY_L.csv (plus M&M, BAJAJ-AUTO and two BE rows, see trim_equity_l).
SCANNER_KEEP = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "BHARTIARTL", "ITC", "LT", "AXISBANK",
    "KOTAKBANK", "HINDUNILVR", "M&M", "BAJAJ-AUTO", "MARUTI", "TATAMOTORS", "TATASTEEL", "SUNPHARMA",
    "NTPC", "POWERGRID", "ONGC", "COALINDIA", "WIPRO", "HCLTECH", "ADANIENT", "ADANIPORTS", "JSWSTEEL",
    "TITAN", "ULTRACEMCO", "BAJFINANCE", "ASIANPAINT", "NESTLEIND", "HINDALCO", "GRASIM", "CIPLA",
    "DRREDDY", "EICHERMOT", "HEROMOTOCO", "VEDL", "IDEA",
]

REGISTRY = {
    "nse-scanner": {
        "files": [
            (NSE_EQUITY_L, "EQUITY_L.csv"),
            (YAHOO + "/v7/finance/spark?symbols={symbols}&range=5d&interval=5m", "spark-5m-{day}.json"),
            (YAHOO + "/v8/finance/chart/{sym}?period1={p1}&period2={p2}&interval=5m", "chart-5m-{day}.json"),
            (YAHOO + "/v7/finance/spark?symbols={symbols}&range=3mo&interval=1d", "daily-spark-{day}.json"),
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


EXPECTED = {"nse-scanner": scanner_expected}


# ---------------------------------------------------------------- driver

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
    symbols = []
    written = []
    for template, fname in spec["files"]:
        name = fname.format(day=day.isoformat())
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
        else:
            text = get(url, pause)
            if name == "EQUITY_L.csv":
                text = trim_equity_l(text)
                symbols = eq_symbols(text)
            written.append(write_fixture(outdir, name, text))
            continue
        if "-5m-" in name:
            body = trim_to_day(body, day)
        elif "daily-" in name:
            body = trim_to_day(body, day, keep_before=True)
        written.append(write_fixture(outdir, name, json.dumps(body, separators=(",", ":"))))
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
