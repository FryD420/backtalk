# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The escape hatch: a dropped conversation must be recoverable.

Why this exists. The resume gate (test_resume_gate.py) stops a cold
overnight session from killing the launch, and it is the right default.
But it makes a decision FOR the person, silently, and sometimes it makes
the wrong one --- a long working session that ran past the window is
exactly the conversation you most want back.

Two things were missing.

FIRST, nothing said anything. A fresh start looked identical to a
resumed one from the outside: same greeting, no mention that yesterday's
conversation had just been let go.

SECOND, and this is the landmine: by the time you could ask for it back,
it was gone. `_remember_session` overwrites .backtalk_session with the
NEW session id on the first completed turn --- seconds after launch. So
the id you would want to recover is destroyed almost immediately, which
is why recovering it by hand on 2026-09-03 meant renaming the file
before saying anything at all.

So the declined id is parked in a sidecar file at the moment it is
refused, before the new session can stomp on it, and a spoken verb
brings it back.

Asserts:
  1. the decision reports BOTH what it resumed and what it refused
  2. the refused id survives the new session overwriting the main file
  3. every flavour of missing/empty/unreadable sidecar means "nothing to
     bring back", never a crash --- same law as the resume gate
  4. the spoken verb is exact-phrase only, like every console verb
  5. reattach() consumes the id through the real start() path

Run:  .venv/Scripts/python tests/test_reattach_hatch.py
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtalk.brain import (  # noqa: E402
    RESUME_MAX_AGE_S,
    WarmBrain,
    clear_declined,
    declined_path,
    load_declined,
    load_resume_id,
    park_declined,
    resume_decision,
)
from backtalk.main import console_match  # noqa: E402

failures = []


def check(name, ok):
    print(("  ok   " if ok else "  FAIL ") + name)
    if not ok:
        failures.append(name)


def saved(sid, age_s, tmp, name=None):
    """A .backtalk_session file written `age_s` seconds ago."""
    p = os.path.join(tmp, name or f"sess-{abs(hash((sid, age_s))) % 10**8}")
    with open(p, "w") as f:
        f.write(sid)
    when = time.time() - age_s
    os.utime(p, (when, when))
    return p


def main():
    tmp = tempfile.mkdtemp(prefix="backtalk-reattach-")
    old = "663c19e3-6536-4f41-88d4-0df95c3ed3a7"
    new = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"

    # ---- 1. The decision now reports what it REFUSED, not just what it
    #         took. Without this the id is known for one instant inside
    #         a function and then thrown away.
    warm = saved(old, 600, tmp)
    rid, declined, age = resume_decision(warm)
    check("a warm session is resumed", rid == old)
    check("...and nothing was refused", declined is None)
    check("...and its age is reported", age is not None and 500 < age < 700)

    cold = saved(old, 13 * 3600, tmp)
    rid, declined, age = resume_decision(cold)
    check("a cold session is not resumed", rid is None)
    check("...but it is HANDED BACK, not discarded", declined == old)
    check("...with the age that got it refused",
          age is not None and age > RESUME_MAX_AGE_S)

    missing = os.path.join(tmp, "not-here")
    rid, declined, age = resume_decision(missing)
    check("no file at all means nothing to resume", rid is None)
    check("...and nothing to bring back either", declined is None)
    check("...and no age to speak", age is None)

    empty = os.path.join(tmp, "empty")
    open(empty, "w").close()
    rid, declined, _ = resume_decision(empty)
    check("an empty file offers nothing in either direction",
          rid is None and declined is None)

    # The old entry point must keep behaving exactly as it did — the
    # resume gate's twelve checks still run against it.
    check("load_resume_id still resumes a warm session",
          load_resume_id(saved(old, 60, tmp)) == old)
    check("load_resume_id still refuses a cold one",
          load_resume_id(saved(old, 13 * 3600, tmp)) is None)

    # ---- 2. THE LANDMINE. The sidecar has to outlive the new session
    #         writing its own id over the main file.
    session_file = saved(old, 13 * 3600, tmp, name=".backtalk_session")
    _, declined, _ = resume_decision(session_file)
    park_declined(declined, session_file)
    check("the parked id reads back", load_declined(session_file) == old)

    # ...and now the new conversation finishes its first turn.
    with open(session_file, "w") as f:
        f.write(new)
    check("the main file now holds the NEW session",
          load_resume_id(session_file) == new)
    check("the refused conversation is STILL recoverable",
          load_declined(session_file) == old)
    check("the sidecar is beside the session file, not on top of it",
          declined_path(session_file) != session_file)

    # A second cold launch parks the newer casualty; you want the last
    # thing you lost, not the first.
    park_declined(new, session_file)
    check("parking again keeps the newest refusal",
          load_declined(session_file) == new)

    clear_declined(session_file)
    check("a consumed hatch is emptied", load_declined(session_file) is None)
    clear_declined(session_file)
    check("clearing an already-empty hatch does not raise", True)

    # ---- 3. Bookkeeping is never fatal. Same law as the resume gate:
    #         a launch must not die over a sidecar file.
    check("no sidecar means nothing to bring back",
          load_declined(os.path.join(tmp, "never-existed")) is None)

    blankdir = os.path.join(tmp, "blank")
    os.makedirs(declined_path(blankdir), exist_ok=True)
    check("a directory where the sidecar should be means nothing",
          load_declined(blankdir) is None)

    ws = os.path.join(tmp, "whitespace")
    with open(declined_path(ws), "w") as f:
        f.write("  \n ")
    check("a whitespace-only sidecar means nothing", load_declined(ws) is None)

    nl = os.path.join(tmp, "newline")
    with open(declined_path(nl), "w") as f:
        f.write(old + "\n")
    check("a trailing newline is stripped off the recovered id",
          load_declined(nl) == old)

    park_declined(None, os.path.join(tmp, "nothing"))
    check("parking nothing writes nothing",
          load_declined(os.path.join(tmp, "nothing")) is None)

    # ---- 4. The verb. Exact phrases only, or an ordinary sentence
    #         about the last conversation would yank the session.
    check("'bring back the last conversation' is the verb",
          console_match("bring back the last conversation") == "reattach")
    check("'reattach anyway' is the verb",
          console_match("reattach anyway") == "reattach")
    check("'bring it back' is the verb",
          console_match("Bring it back.") == "reattach")
    check("a sentence merely containing the phrase is NOT the verb",
          console_match("could you bring back the last conversation for me")
          is None)
    check("the fresh-start verb is untouched",
          console_match("start fresh") == "clear")

    # ---- 5. reattach() goes through the real start() path, so the
    #         resume-failed fallback inside it still applies.
    brain = WarmBrain(model="claude-opus-5")
    calls = {"stop": 0, "start": 0, "resume_at_start": []}

    async def fake_stop():
        calls["stop"] += 1
        brain._client = None

    async def fake_start():
        calls["start"] += 1
        calls["resume_at_start"].append(brain._resume_id)
        brain.resumed = bool(brain._resume_id)
        brain._resume_id = None

    brain.stop, brain.start = fake_stop, fake_start
    brain._client = object()          # pretend a live connection
    asyncio.run(brain.reattach(old))
    check("reattach tears the live session down first", calls["stop"] == 1)
    check("...then starts one back up", calls["start"] == 1)
    check("...carrying the recovered id", calls["resume_at_start"] == [old])
    check("...and the brain knows it is resumed", brain.resumed is True)

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
