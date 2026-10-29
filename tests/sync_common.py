"""Keep the shared blocks identical across the tools.

The master file is nse-exit/exit.py. Two marked blocks are copied from it:

    # --- nse common: begin ---  ...  # --- nse common: end ---     (every tool)
    # --- nse charges: begin --- ...  # --- nse charges: end ---    (the charge-aware tools)

Usage:
    python3 tests/sync_common.py            rewrite the blocks in every target
    python3 tests/sync_common.py --check    exit 1, listing files whose block differs or is missing

A block the master does not carry yet is skipped, and so is a target file that does not exist.
"""

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join("nse-exit", "exit.py")
SKILLS = ["nse-scanner", "nse-news", "nse-entry", "nse-exit", "nse-timemachine",
          "nse-risk", "nse-budget", "nse-broker", "nse-alerts", "nse-analyst", "nse-deals", "nse-orderbook"]
CHARGE_FILES = [os.path.join("nse-budget", "size.py"), os.path.join("nse-risk", "check.py"),
                os.path.join("nse-timemachine", "timemachine.py"), os.path.join("nse-broker", "paper.py")]


def all_tools():
    out = []
    for s in SKILLS:
        folder = os.path.join(ROOT, s)
        if os.path.isdir(folder):
            out.extend(os.path.join(s, f) for f in sorted(os.listdir(folder)) if f.endswith(".py"))
    out.append(os.path.join("nse-broker", "paper.py"))
    return sorted(set(out))


BLOCKS = [("common", None), ("charges", CHARGE_FILES)]  # (name, targets; None means every tool)


def markers(name):
    return "# --- nse %s: begin ---" % name, "# --- nse %s: end ---" % name


def find(text, name):
    """(start, end) character span of the block including both marker lines, or None."""
    begin, end = markers(name)
    i = text.find(begin)
    j = text.find(end, i + 1) if i >= 0 else -1
    if i < 0 or j < 0:
        return None
    return i, j + len(end)


def insert_at(text):
    """Character offset just after the module's last top-level import (0 if it has none)."""
    tree = ast.parse(text)
    ends = [n.end_lineno for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    if not ends:
        return 0
    lines = text.split("\n")
    return len("\n".join(lines[:max(ends)]))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def main(argv):
    check = "--check" in argv
    master = read(MASTER)
    bad = []
    inserted = set()
    for name, targets in BLOCKS:
        span = find(master, name)
        if span is None:
            continue  # the master has no such block yet
        want = master[span[0]:span[1]]
        for rel in (targets if targets is not None else all_tools()):
            if rel == MASTER or not os.path.isfile(os.path.join(ROOT, rel)):
                continue
            text = read(rel)
            got = find(text, name)
            if got is None:
                bad.append("%s: %s block missing" % (rel, name))
                if not check:
                    cspan = find(text, "common")
                    at = cspan[1] if (name != "common" and cspan) else insert_at(text)
                    with open(os.path.join(ROOT, rel), "w", encoding="utf-8") as fh:
                        fh.write(text[:at] + "\n\n" + want + text[at:])
                    inserted.add(bad[-1])
                continue
            if text[got[0]:got[1]] == want:
                continue
            bad.append("%s: %s block differs" % (rel, name))
            if not check:
                with open(os.path.join(ROOT, rel), "w", encoding="utf-8") as fh:
                    fh.write(text[:got[0]] + want + text[got[1]:])
    if check:
        if bad:
            print("\n".join(bad))
            return 1
        print("common blocks identical")
        return 0
    print("\n".join(("inserted " if b in inserted else "updated ") + b for b in bad) if bad else "nothing to sync")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
