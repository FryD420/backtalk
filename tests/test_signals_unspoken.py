# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""signals.unspoken: the dashboard file for text that was not spoken.

Why this exists. Agent text that reached the voice line but was never
said (background news while speech is off, the rest of a reply that was
cut off, sentences over the cap, whatever was queued at shutdown) used
to vanish into the log, or not even that. It now lands in
.voice_unspoken, one JSON line each, for a face to show.

Asserts:
  1. one JSON line per record: {ts, origin, reason, what, partial, text}
  2. text is whitespace-collapsed; partial is a real bool
  3. past 256 KB the file is trimmed to its newest 200 lines
  4. like every bus write, it never raises (an unwritable path)

Run:  .venv/Scripts/python tests/test_signals_unspoken.py
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
    tmp = tempfile.mkdtemp(prefix="backtalk-unspoken-")
    path = os.path.join(tmp, ".voice_unspoken")
    signals._UNSPOKEN_FILE = path

    signals.unspoken("bg", "capped", "News five.\n  News six.",
                     what="the build-the-APK agent")
    signals.unspoken("fg", "interrupted", "Second sentence.", partial=True)
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    check("1. one line per record", len(lines) == 2)
    rec = json.loads(lines[0])
    check("1. the line has exactly the documented keys",
          set(rec) == {"ts", "origin", "reason", "what", "partial", "text"})
    check("1. the values are what was passed",
          rec["origin"] == "bg" and rec["reason"] == "capped"
          and rec["what"] == "the build-the-APK agent"
          and isinstance(rec["ts"], float))
    check("2. text is whitespace-collapsed",
          rec["text"] == "News five. News six.")
    rec2 = json.loads(lines[1])
    check("2. partial is a real bool", rec2["partial"] is True
          and rec["partial"] is False and rec2["what"] is None)

    big = "x" * 1000
    for i in range(300):                 # ~310 KB of 1 KB lines
        signals.unspoken("bg", "speech_off", f"{i} {big}")
    size = os.path.getsize(path)
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    check("3. trimmed once past 256 KB", size < 256 * 1024 + 4096)
    check("3. ...to its newest 200 lines, then growing again",
          200 <= len(lines) < 300)
    check("3. ...keeping the newest record last",
          json.loads(lines[-1])["text"].startswith("299 "))
    check("3. every surviving line is still valid JSON",
          all(json.loads(x)["reason"] for x in lines))

    signals._UNSPOKEN_FILE = tmp          # a directory: cannot be opened
    try:
        signals.unspoken("bg", "shutdown", "anything")
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
