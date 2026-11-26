#!/usr/bin/env python3
"""Append a single reviewable decision without rewriting earlier rows."""

import csv
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


def main():
    if len(sys.argv) != 7:
        raise SystemExit("usage: log_decision.py logfile phase decision why evidence result")
    path = Path(sys.argv[1])
    values = [datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"), *sys.argv[2:]]
    values = [" ".join(value.split()) for value in values]
    values = [("'" + value) if value.startswith(("=", "+", "-", "@")) else value for value in values]
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        if needs_header:
            writer.writerow(["ts", "phase", "decision", "why", "evidence", "result"])
        writer.writerow(values)


if __name__ == "__main__":
    main()
