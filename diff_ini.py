# -*- coding: utf-8 -*-
"""Section/key-aware diff: generated -pre.ini vs golden reference .ini"""
import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")


def parse(path):
    """-> {section: {key: value}}  ignoring comments/blank lines"""
    data, sec = {}, None
    for raw in open(path, encoding="utf-8-sig"):
        line = raw.rstrip("\n")
        s = line.strip()
        if not s or s.startswith(";"):
            continue
        m = re.match(r"^\[(.+)\]$", s)
        if m:
            sec = m.group(1)
            data.setdefault(sec, {})
            continue
        if "=" in s and sec is not None:
            k, v = s.split("=", 1)
            data[sec][k.strip()] = v.strip()
    return data


def diff(golden_path, result_path):
    g = parse(golden_path)
    r = parse(result_path)

    match = mismatch = missing_real = missing_empty = extra = 0
    all_secs = list(dict.fromkeys(list(g.keys()) + list(r.keys())))

    for sec in all_secs:
        gk, rk = g.get(sec, {}), r.get(sec, {})
        keys = list(dict.fromkeys(list(gk.keys()) + list(rk.keys())))
        seclines = []
        for k in keys:
            gv, rv = gk.get(k), rk.get(k)
            if gv is not None and rv is not None:
                if gv == rv:
                    match += 1
                else:
                    mismatch += 1
                    seclines.append("  ~ %-12s golden: %s" % (k, gv))
                    seclines.append("  %-14s ours:   %s" % ("", rv))
            elif gv is not None and rv is None:
                if gv == "":
                    missing_empty += 1
                else:
                    missing_real += 1
                    seclines.append("  - %-12s MISSING-REAL (golden: %s)" % (k, gv))
            elif gv is None and rv is not None:
                extra += 1
                seclines.append("  + %-12s EXTRA (ours: %s)" % (k, rv))
        if seclines:
            print("[%s]" % sec)
            print("\n".join(seclines))
            print()

    print("=" * 64)
    meaningful = match + mismatch + missing_real + extra
    print("MEANINGFUL keys (non-empty in golden, or emitted by us):")
    print("  exact match : %d" % match)
    print("  mismatch    : %d" % mismatch)
    print("  missing-real: %d  (golden has a real value, we omitted)" % missing_real)
    print("  extra       : %d  (we emitted, golden has none)" % extra)
    print("  --> meaningful accuracy: %d/%d = %.1f%%" %
          (match, meaningful, 100.0 * match / meaningful if meaningful else 0))
    print()
    print("empty placeholder slots golden emits but we skip: %d (format-only, not errors)" % missing_empty)


def main():
    parser = argparse.ArgumentParser(description="Diff generated INI against a golden reference.")
    parser.add_argument("--project", help="Project name (e.g. SOM-6884)")
    parser.add_argument("--golden",  help="Path to golden .ini (overrides --project default)")
    parser.add_argument("--result",  help="Path to generated -pre.ini (overrides --project default)")
    parser.add_argument("--root",    default=".", help="Base directory for relative paths")
    args = parser.parse_args()

    root = args.root

    if args.golden:
        golden_path = args.golden
    elif args.project:
        golden_path = os.path.join(root, "00_SusiEditor", "00_SusiEditor", f"{args.project}.ini")
    else:
        parser.error("Provide --golden or --project")

    if args.result:
        result_path = args.result
    elif args.project:
        result_path = os.path.join(root, f"{args.project}-pre.ini")
    else:
        parser.error("Provide --result or --project")

    diff(golden_path, result_path)


if __name__ == "__main__":
    main()
