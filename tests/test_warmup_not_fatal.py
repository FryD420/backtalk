# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A slow warmup must never kill the voice line.

Why this exists. The startup warmup is optional. On a fresh session it
is a silent ping; on a reattached one it is the spoken where-were-we
recap. Neither is the product --- they are pleasantries before the
first real question.

Until 2026-09-03 a warmup that ran long called SystemExit(1). The whole
voice line died over a pleasantry: mic gone, inbox on 8795 gone, the
GUI's panels left staring at nothing, and the watchdog correctly
declining to resurrect any of it. That morning it happened four times
between 10:41 and 11:01 and the machine simply had no voice.

The old behaviour was not stupid, and the reason is worth keeping: an
EARLIER bug had the greeting play and then nothing happen, silently, so
whoever wrote this chose to die loudly rather than lie. The fix keeps
the loud and drops the dying. Say what went wrong out loud, drop the
reattachment, come up on a fresh conversation, and let the person talk.

A failed CONNECT is still fatal, and should be --- no brain at all means
nothing works. This is only about the warmup on top of a live one.

Asserts:
  1. a warmup that completes is left entirely alone
  2. a warmup that hangs past the timeout does NOT raise --- the voice
     line survives, which is the whole point
  3. after a hung warmup on a REATTACHED session, the reattachment is
     dropped and a fresh session is started, and the person is told
  4. the same on a FRESH session warns but does not restart anything
  5. if even the fresh restart fails, it still does not raise
  6. a warmup that raises (a 529, a dropped socket) is handled the same
     way as one that hangs

Run:  .venv/Scripts/python tests/test_warmup_not_fatal.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtalk.brain import warmup_or_fresh  # noqa: E402

failures = []


def check(name, ok):
    print(("  ok   " if ok else "  FAIL ") + name)
    if not ok:
        failures.append(name)


class FakeBrain:
    """Just the surface warmup_or_fresh is allowed to touch."""

    def __init__(self, resumed=False, restart_raises=False):
        self.resumed = resumed
        self.restart_raises = restart_raises
        self.stopped = 0
        self.started = 0

    async def stop(self):
        self.stopped += 1

    async def start(self):
        self.started += 1
        if self.restart_raises:
            raise RuntimeError("no brain at all")


class FakeMouth:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)


def run(coro):
    return asyncio.run(coro)


def main():
    # 1. The happy path is untouched.
    brain, mouth = FakeBrain(resumed=True), FakeMouth()

    async def quick():
        return "warm"

    ok = run(warmup_or_fresh(brain, mouth, quick, timeout=5))
    check("a completed warmup reports success", ok is True)
    check("...and nothing was restarted",
          brain.stopped == 0 and brain.started == 0)
    check("...and nothing was said about it", mouth.said == [])

    # 2. THE REGRESSION. A hung warmup must not take the process with it.
    brain, mouth = FakeBrain(resumed=True), FakeMouth()

    async def hangs():
        await asyncio.sleep(30)

    raised = None
    try:
        ok = run(warmup_or_fresh(brain, mouth, hangs, timeout=0.2))
    except BaseException as e:          # SystemExit is not an Exception
        raised = e
        ok = None
    check("a hung warmup does not raise (the voice line survives)",
          raised is None)
    check("...and it reports failure", ok is False)

    # 3. The reattachment is what went wrong, so it is what gets dropped.
    check("the stale session was torn down", brain.stopped == 1)
    check("a fresh session was started", brain.started == 1)
    check("the brain no longer claims to be resumed", brain.resumed is False)
    check("the person is told out loud", len(mouth.said) == 1)
    check("...in plain language, not a stack trace",
          bool(mouth.said) and "fresh" in mouth.said[0].lower())

    # 4. A fresh session has nothing to drop; warn, restart nothing.
    brain, mouth = FakeBrain(resumed=False), FakeMouth()
    ok = run(warmup_or_fresh(brain, mouth, hangs, timeout=0.2))
    check("a fresh-session warmup failure does not raise either",
          ok is False)
    check("...and nothing is torn down or rebuilt",
          brain.stopped == 0 and brain.started == 0)
    check("...but the person is still warned", len(mouth.said) == 1)

    # 5. Belt and braces: even a failed rescue must not raise.
    brain = FakeBrain(resumed=True, restart_raises=True)
    mouth = FakeMouth()
    raised = None
    try:
        ok = run(warmup_or_fresh(brain, mouth, hangs, timeout=0.2))
    except BaseException as e:
        raised = e
        ok = None
    check("a failed fresh restart still does not raise", raised is None)
    check("...and it reports failure", ok is False)
    check("...and the person hears about it", len(mouth.said) >= 1)

    # 6. An exception (a 529, a dropped socket) is not different from a
    #    hang as far as the voice line is concerned.
    brain, mouth = FakeBrain(resumed=True), FakeMouth()

    async def explodes():
        raise RuntimeError("API Error: 529 Overloaded")

    raised = None
    try:
        ok = run(warmup_or_fresh(brain, mouth, explodes, timeout=5))
    except BaseException as e:
        raised = e
        ok = None
    check("a warmup that raises does not raise onward", raised is None)
    check("...and it reports failure", ok is False)
    check("...and it recovers the same way",
          brain.stopped == 1 and brain.started == 1)

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
