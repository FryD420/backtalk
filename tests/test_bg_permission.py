# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A background turn asks permission: the real gate, the real reader.

Why this exists. Before the single pipe reader, a background turn's
permission request could not even arrive: the SDK's buffer was full and
its reader blocked, so the request sat unanswered while the turn froze.
Now it arrives, and the live probe on 2026-10-08 could not exercise it
(the CLI auto-allowed the probe's read-only commands). So it is covered
here, with the fake CLI: the SDK calls can_use_tool (the spoken gate,
main.make_permission_gate) while a background turn is open.

Asserts:
  1. the question opens with "A background job needs a yes or no."
  2. it takes the floor from news already playing, at the next chunk
     boundary (the playing chunk finishes, the rest waits)
  3. the reader keeps reading while the question waits (no stall), and
     the news that arrives meanwhile is held, not spoken over it
  4. an exact "yes" approves; afterwards the held news plays, in order
  5. a "no, ..." denies with the person's words as the reason
  6. a background question while an ANSWER is still playing: the gate
     goes next, the answer resumes after it

Run:  .venv/Scripts/python tests/test_bg_permission.py
"""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakes  # noqa: E402
from fakes import (FakeClient, FakeMouth, NOTIF, Unspoken,  # noqa: E402
                   check_factory, message_start, result, task_done,
                   task_started, text)

import backtalk.brain as brain_mod  # noqa: E402
import backtalk.main as main  # noqa: E402
from backtalk.floor import Chunker, Floor  # noqa: E402

# no thinking sound, no bus state from a unit test
main.signals.static_start = lambda: None
main.signals.static_stop = lambda: None
main.signals.set_state = lambda s: None
brain_mod.ClaudeSDKClient = FakeClient

failures: list = []
check = check_factory(failures)

CTX = types.SimpleNamespace(display_name=None, description=None)


def perm_pending():
    f = main._PERM["fut"]
    return f is not None and not f.done()


async def pump_until(floor, mouth, cond, limit=200):
    """Run the floor like floor.run() does, playing chunks as they come."""
    for _ in range(limit):
        while not floor.brain.events.empty():
            floor.handle_event(floor.brain.events.get_nowait())
        floor.pump()
        if cond():
            return True
        await asyncio.sleep(0.005)
    return cond()


async def scenario():
    FakeClient.instances.clear()
    main._PERM.update(fut=None, asked_at=0.0, hinted=True)
    main._AUTOAPPROVE["on"] = False
    mouth = FakeMouth()
    floor = Floor(mouth, user_active=lambda: False,
                  perm_pending=perm_pending,
                  cfg={"background_speech": "interrupt"},
                  unspoken=Unspoken(), logf=lambda s: None)
    gate = main.make_permission_gate(floor)
    b = brain_mod.WarmBrain(model="claude-test", can_use_tool=gate)
    floor.brain = b
    await b.start()
    c = FakeClient.instances[-1]

    def hook(cl, p):
        cl.consume(p)
        cl.push(message_start(), text("Ready."), result())
    FakeClient.on_query = hook
    async for _ in b.ask_stream("Warmup ping"):
        pass

    # ---- a background agent finishes and its turn starts talking
    c.push(task_started("t1", "Ship the release", "local_agent"),
           task_done("t1"), message_start(),
           text("The release agent is done. Pushing the tag now."))
    await pump_until(floor, mouth, lambda: len(mouth.queue) >= 2)
    check("news is playing", mouth.queue[0].text.startswith("Heads up."))
    check("the open turn is background", b.open_origin == "bg")

    # ---- ...and it asks permission (the SDK calls can_use_tool)
    playing = mouth.queue[0].text
    asking = asyncio.create_task(
        gate("Bash", {"command": "git push origin v2"}, CTX))
    await pump_until(floor, mouth, perm_pending)
    check("2. the playing chunk was left alone",
          mouth.queue[0].text == playing)
    check("1. the question is next, and says a background job is asking",
          [r.text for r in mouth.queue[1:]][:1]
          == ["A background job needs a yes or no."])
    check("2. the news not yet started was pulled back",
          mouth.yields >= 1)

    # ---- the reader keeps reading while the question waits
    before = c.iterated
    c.push(text("Tag pushed. " * 150))
    await pump_until(floor, mouth, lambda: c.iterated - before >= 150)
    check("3. 150+ frames read while the question waited",
          c.iterated - before >= 152)
    while mouth.queue:                    # everything queued plays out
        mouth.finish()
        floor.pump()
    heard = mouth.heard()
    check("3. nothing but the question was spoken while it waited",
          not any(h.startswith("Tag pushed") for h in heard)
          and heard[-1] == "Yes, no, or details?")

    # ---- "yes"
    main._PERM["fut"].set_result("yes")
    res = await asyncio.wait_for(asking, 2)
    check("4. an exact yes approves",
          type(res).__name__ == "PermissionResultAllow")
    c.push(result(origin=NOTIF))
    await pump_until(floor, mouth, lambda: False, limit=20)
    while mouth.queue:
        mouth.finish()
        floor.pump()
    after = mouth.heard()[len(heard):]
    # the news pulled back for the question comes back first, then the
    # rest, capped at background_max_sentences (4) with rest_on_screen
    check("4. after the answer the held news plays, in order",
          after == ["The release agent is done.",
                    "Pushing the tag now. Tag pushed.", "Tag pushed.",
                    "The rest is on the screen."])

    # ---- "no, ..." from a second background ask
    c.push(message_start(), text("One more thing to do."))
    await pump_until(floor, mouth, lambda: b.open_origin == "bg")
    asking = asyncio.create_task(
        gate("Bash", {"command": "rm -rf build"}, CTX))
    await pump_until(floor, mouth, perm_pending)
    main._PERM["fut"].set_result("no, leave the build folder alone")
    res = await asyncio.wait_for(asking, 2)
    check("5. anything else denies, with the words as the reason",
          type(res).__name__ == "PermissionResultDeny"
          and "leave the build folder alone" in res.message)
    c.push(result(origin=NOTIF))
    await pump_until(floor, mouth, lambda: b.open_origin is None)

    # ---- 6. an answer still playing when a background job asks
    while mouth.queue:
        mouth.finish()
        floor.pump()
    mark = len(mouth.heard())
    tok = floor.fg_begin()
    ch = Chunker(lambda t, d: floor.fg_chunk(t, d, tok))
    for s in ("First.", "Second.", "Third.", "Fourth.", "Fifth."):
        ch.feed(s)
    ch.flush()
    floor.fg_done(tok)
    c.push(message_start(), text("Background again."))
    await pump_until(floor, mouth, lambda: b.open_origin == "bg")
    asking = asyncio.create_task(gate("WebFetch",
                                      {"url": "https://example.com/x"}, CTX))
    await pump_until(floor, mouth, perm_pending)
    main._PERM["fut"].set_result("yes")
    await asyncio.wait_for(asking, 2)
    c.push(text("All fetched."), result(origin=NOTIF))
    await pump_until(floor, mouth, lambda: b.open_origin is None
                     and b.events.empty())
    for _ in range(40):
        floor.pump()
        if not mouth.queue:
            break
        mouth.finish()
        await pump_until(floor, mouth, lambda: True, limit=1)
    heard = mouth.heard()[mark:]
    q = heard.index("A background job needs a yes or no.")
    check("6. the gate went in at a chunk boundary, the answer after it",
          heard[0] == "First." and heard[q + 1] == "Permission check."
          and "Fourth. Fifth." in heard[q:])
    check("6. the news it was asking for was told, then the answer resumed",
          "Background again." in heard and "All fetched." in heard
          and heard.index("Right, back to what I was saying.")
          < heard.index("Second. Third."))
    check("6. every answer chunk spoken exactly once",
          all(heard.count(x) == 1 for x in
              ("First.", "Second. Third.", "Fourth. Fifth.")))

    FakeClient.on_query = None
    await b.stop()


def main_():
    try:
        asyncio.run(asyncio.wait_for(scenario(), 30))
    except Exception as e:
        import traceback
        traceback.print_exc()
        check(f"the scenario ran without raising ({e!r})", False)
    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        for f in failures:
            print("  - " + f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_())
