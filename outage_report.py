#!/usr/bin/env python3
"""Summarise power outages and farm internet outages from events.jsonl.

Usage:  python3 outage_report.py [--days 30] [--file events.jsonl]
"""
import argparse
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def fmt(sec):
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h} h {m:02d} min {s:02d} s" if h else f"{m} min {s:02d} s"


def report(events, kind, title):
    rows = [e for e in events if e["kind"] == kind and e.get("duration_sec") is not None]
    print(f"\n{title}: {len(rows)}")
    for e in rows:
        start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["start"]))
        note = "  (upper bound)" if e.get("note") else ""
        print(f"  {start}  {fmt(e['duration_sec'])}{note}")
    if rows:
        total = sum(e["duration_sec"] for e in rows)
        print(f"  Total: {fmt(total)}   Longest: {fmt(max(e['duration_sec'] for e in rows))}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--file", default=os.path.join(HERE, "events.jsonl"))
    args = ap.parse_args()
    cutoff = time.time() - args.days * 86400
    events = []
    with open(args.file) as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e["ts"] >= cutoff:
                events.append(e)
    print(f"Last {args.days} days, from {args.file}")
    report(events, "power_restored", "Power outages")
    report(events, "internet_restored", "Farm internet outages")


if __name__ == "__main__":
    main()
