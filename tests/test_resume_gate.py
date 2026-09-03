# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The resume gate: never reattach to a conversation that has gone cold.

Why this exists. On 2026-09-03 the voice line would not start. Four
launches in a row logged BRAIN CONNECT timed out and killed themselves,
and the GUI sat there with nothing to show.

The cause was `resume_last_session` doing exactly what it was told. It
reattaches to whatever session id was last written, however old --- and
overnight that id had grown into a 2,345-entry, 4.2 MB conversation. A
launch the next morning replays the whole thing through an expired
prompt cache, which is the single most expensive request this machine
ever makes. It did not come back inside the startup guard. (Anthropic
were also having an incident that morning, which is why it tipped over
that day rather than some other day --- but the request was always going
to be a coin flip, and the design is what made it fatal.)

The feature is still worth having: relaunch mid-session and it picks
straight up. So the fix is not to remove it, it is to AGE it. A session
touched minutes ago is warm and cheap to resume. One touched last night
is a cold replay of everything, and starting fresh is both faster and
better --- the vault carries yesterday, not the transcript.

Asserts:
  1. a session saved moments ago is reattached
  2. a session older than the window is skipped
  3. the boundary is inclusive-ish: just inside resumes, just outside does not
  4. a missing file, an empty file and an unreadable one all mean "fresh",
     never a crash --- a launch must never die over a bookkeeping file

Run:  .venv/Scripts/python tests/test_resume_gate.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtalk.brain import RESUME_MAX_AGE_S, load_resume_id  # noqa: E402

failures = []


def check(name, ok):
    print(("  ok   " if ok else "  FAIL ") + name)
    if not ok:
        failures.append(name)


def saved(sid, age_s, tmp):
    """A .backtalk_session file written `age_s` seconds ago."""
    p = os.path.join(tmp, f"sess-{abs(hash((sid, age_s))) % 10**8}")
    with open(p, "w") as f:
        f.write(sid)
    when = time.time() - age_s
    os.utime(p, (when, when))
    return p


def main():
    tmp = tempfile.mkdtemp(prefix="backtalk-resume-gate-")
    sid = "663c19e3-6536-4f41-88d4-0df95c3ed3a7"

    # 1. Warm: the mid-session relaunch this feature exists for.
    check("a session saved 60s ago is reattached",
          load_resume_id(saved(sid, 60, tmp)) == sid)
    check("a session saved 10 minutes ago is reattached",
          load_resume_id(saved(sid, 600, tmp)) == sid)

    # 2. Cold: the overnight case that took the voice line down.
    check("a session saved 13 hours ago is skipped",
          load_resume_id(saved(sid, 13 * 3600, tmp)) is None)
    check("a session saved a week ago is skipped",
          load_resume_id(saved(sid, 7 * 86400, tmp)) is None)

    # 3. The boundary itself.
    check("just inside the window resumes",
          load_resume_id(saved(sid, RESUME_MAX_AGE_S - 60, tmp)) == sid)
    check("just outside the window does not",
          load_resume_id(saved(sid, RESUME_MAX_AGE_S + 60, tmp)) is None)
    check("the window is overridable per call",
          load_resume_id(saved(sid, 600, tmp), max_age_s=60) is None)

    # 4. Bookkeeping must never be fatal. Every one of these is a launch
    #    that has to end in a working voice line, not a traceback.
    check("a missing file means fresh",
          load_resume_id(os.path.join(tmp, "not-here")) is None)

    empty = os.path.join(tmp, "empty")
    open(empty, "w").close()
    check("an empty file means fresh", load_resume_id(empty) is None)

    blank = os.path.join(tmp, "blank")
    with open(blank, "w") as f:
        f.write("   \n  ")
    check("a whitespace-only file means fresh", load_resume_id(blank) is None)

    check("a directory where the file should be means fresh",
          load_resume_id(tmp) is None)

    # The id is returned stripped — a trailing newline must not become
    # part of the session id handed to the SDK.
    nl = os.path.join(tmp, "newline")
    with open(nl, "w") as f:
        f.write(sid + "\n")
    now = time.time()
    os.utime(nl, (now, now))
    check("a trailing newline is stripped", load_resume_id(nl) == sid)

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
