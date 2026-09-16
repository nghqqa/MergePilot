#!/usr/bin/env python3
"""Poll team room + leader DM and print new m.room.message events since a UTC timestamp.
Usage: python monitor.py <since-ISO-UTC> [max_seconds] [interval]
Stops early when a message body contains one of the STOP markers (or on timeout).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import matrix as mx  # noqa: E402

TEAM = "!RErK7WVs9iUeaszwho:elemiso-matrix:6167"
DM = "!VocOLrDhUMBcBjzIuY:elemiso-matrix:6167"
STOP = [s for s in os.environ.get("MON_STOP", "").split("|") if s]


def main():
    since = sys.argv[1]
    max_s = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    interval = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    seen = set()
    t_end = time.time() + max_s
    stop_hit = None
    while True:
        for label, rid in (("TEAM", TEAM), ("DM", DM)):
            try:
                evs = mx.since(rid, since)
            except Exception as e:
                print(f"[{label}] poll error {type(e).__name__}")
                continue
            for ev in evs:
                if ev["event_id"] in seen:
                    continue
                seen.add(ev["event_id"])
                body = ev["body"].replace("\n", " ⏎ ")
                print(f"[{label}] {ev['ts']} {ev['sender'].split(':')[0]} {ev['event_id'][:14]} mentions={ev['mentions']} :: {body[:420]}")
                for s in STOP:
                    if s in ev["body"]:
                        stop_hit = s
        sys.stdout.flush()
        if stop_hit or time.time() >= t_end:
            break
        time.sleep(interval)
    print(f"--- monitor end {time.strftime('%H:%M:%SZ', time.gmtime())} stop={stop_hit} events={len(seen)}")


if __name__ == "__main__":
    main()
