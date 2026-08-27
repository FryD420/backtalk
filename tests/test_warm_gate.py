# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The warm gate: nothing is queued into a voice that cannot speak yet.

Why this exists. Loading Kokoro takes ~30 s on this machine. The greeting
and the resume recap used to be queued immediately, so they sat silent for
that whole window — and a talk-key press in that window calls
mouth.shut_up(), which flushes the queue. On 2026-08-26 both the greeting
and the where-were-we recap were destroyed without ever making a sound and
the session simply looked mute.

The fix is not "make the flush smarter": it is to never queue speech into a
cold mouth. Then a press during warm-up destroys nothing, because there is
nothing queued yet to destroy.

Asserts:
  1. the gate starts closed and wait_warm() times out while it is
  2. warm() opening the gate releases a waiter, and later waits are free
  3. speech deferred behind the gate survives a shut_up() that happens
     during warm-up — the exact sequence that lost the recap

Run:  .venv/Scripts/python tests/test_warm_gate.py
"""
import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backtalk  # noqa: E402

# Kokoro and sounddevice have no place in a unit check; stub the module's
# heavy edges before importing it.
sys.modules.setdefault("sounddevice", types.SimpleNamespace(
    OutputStream=object, query_devices=lambda *a, **k: {}))

from backtalk import mouth as M  # noqa: E402

failures = []


def check(name, ok):
    print(("  ok   " if ok else "  FAIL ") + name)
    if not ok:
        failures.append(name)


def main():
    # 1. Closed at rest.
    M._warm_event.clear()
    check("gate starts closed", not M.is_warm())
    t0 = time.monotonic()
    check("wait_warm times out while cold", M.wait_warm(0.15) is False)
    check("...and it actually waited", time.monotonic() - t0 >= 0.1)

    # 2. Opening it releases a waiter.
    released = threading.Event()

    def waiter():
        if M.wait_warm(5):
            released.set()

    threading.Thread(target=waiter, daemon=True).start()
    time.sleep(0.05)
    check("waiter still blocked while cold", not released.is_set())
    M._warm_event.set()
    check("waiter released once warm", released.wait(2))
    check("is_warm reports true", M.is_warm())
    t0 = time.monotonic()
    check("a later wait returns immediately",
          M.wait_warm(5) and time.monotonic() - t0 < 0.1)

    # 3. THE REGRESSION. A talk-key press during warm-up flushes the
    #    queue; deferred speech must outlive it, because it was never
    #    queued in the first place.
    M._warm_event.clear()
    spoken = []
    fake_mouth = types.SimpleNamespace(
        say=lambda text: spoken.append(text),
        shut_up=lambda: spoken.clear(),
    )

    def greet_when_warm():
        if M.wait_warm(5):
            fake_mouth.say("greeting")

    threading.Thread(target=greet_when_warm, daemon=True).start()
    time.sleep(0.05)
    fake_mouth.shut_up()          # the talk-key press, mid-warm-up
    check("nothing was queued to destroy", spoken == [])
    M._warm_event.set()           # the voice finishes loading
    deadline = time.monotonic() + 2
    while not spoken and time.monotonic() < deadline:
        time.sleep(0.01)
    check("the greeting survived the press and spoke", spoken == ["greeting"])

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
