# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""signals.heard: the dashboard file for background news that was heard.

Why this exists. A face that lists finished background agents needs to
know which ones the person has actually HEARD about, so it never throws
away news nobody heard (jarvis-gui's agent roster, Joe 2026-10-13). The
floor calls signals.heard(task_id, label) when a background turn's news
was spoken to the end; it lands in .voice_heard, one JSON line each.

Asserts:
  1. one JSON line per record: exactly {ts, task_id, label}
  2. an empty or missing task id writes nothing
  3. past 64 KB the file is trimmed to its newest 500 lines
  4. like every bus write, it never raises (an unwritable path)

Run:  .venv/Scripts/python tests/test_signals_heard.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtalk import signals  # noqa: E402

failures = []


def check(name, ok):
    print(("  ok   " if ok else "  FAIL ") + name)
    if not ok:
        failures.append(name)


def main():
    tmp = tempfile.mkdtemp(prefix="backtalk-heard-")
    path = os.path.join(tmp, ".voice_heard")
    signals._HEARD_FILE = path

    signals.heard("a0038b2113e7f9544", "Build the APK")
    signals.heard("b04n8qwcj", None)
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    check("1. one line per record", len(lines) == 2)
    rec = json.loads(lines[0])
    check("1. the line has exactly the documented keys",
          set(rec) == {"ts", "task_id", "label"})
    check("1. the values are what was passed",
          rec["task_id"] == "a0038b2113e7f9544"
          and rec["label"] == "Build the APK"
          and isinstance(rec["ts"], float))
    check("1. a missing label is null", json.loads(lines[1])["label"] is None)

    signals.heard("", "nothing")
    signals.heard(None, "nothing")
    with open(path, encoding="utf-8") as f:
        check("2. an empty or missing task id writes nothing",
              len(f.read().splitlines()) == 2)

    pad = "x" * 20
    for i in range(1000):                # ~85 KB of ~85-byte lines
        signals.heard(f"id{i}", f"{i} {pad}")
    size = os.path.getsize(path)
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    check("3. trimmed once past 64 KB", size < 64 * 1024 + 4096)
    check("3. ...to its newest 500 lines, then growing again",
          500 <= len(lines) < 800)
    check("3. ...keeping the newest record last",
          json.loads(lines[-1])["task_id"] == "id999")
    check("3. every surviving line is still valid JSON",
          all(json.loads(x)["task_id"] for x in lines))

    signals._HEARD_FILE = tmp             # a directory: cannot be opened
    try:
        signals.heard("a1", "anything")
        ok = True
    except Exception:
        ok = False
    check("4. an unwritable path never raises", ok)

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        for f in failures:
            print("  - " + f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
