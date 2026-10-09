# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The single pipe reader: one owner of the SDK message stream.

Why this exists. On 2026-10-07 at 23:40 "Everything okay?" got back
"The app side is committed and pushed. Exporting the app now." -- a
stale answer from a background turn, at 0.0s to first. The real news
("the build is on your S23") was dropped unspoken twice. The pipe was
only read while one of OUR turns ran, so a background turn filled the
SDK's 100-message buffer, froze, and the rest of it was paired with
the next question. brain._read_pipe now reads every frame the moment
it lands and attributes every turn by the CLI's echo of each prompt.

Asserts (spec section 7, test_pipe_reader):
  1. the 2026-10-07 regression: 250 background frames, then a question
     -> only the answer is yielded, the background text went to events
  2. only _read_pipe touches receive_messages/receive_response, and the
     fake never saw two iterators open at once
  3. a question folded into an open background turn: text after the
     echo is the answer, text before it is background
  4. no echo (old CLI): a question during an open background turn is
     held, then sent after fg_hold_s; nothing lost, nothing duplicated
  5. a slow (5 s) rate-limit pull while 300 frames arrive: all read
  6. reset_turn interrupts and waits for the turn to close, no draining;
     on timeout it rebuilds, old reader cancelled before the new starts
  7. sub-agent frames are never yielded and never open a turn
  8. the reader dies -> brain marked lost, next question rebuilds
  9. slash commands pair with or without an echo of their own
 10. a turn that answered our prompt without a matching echo ends the
     question instead of hanging it forever
 11. a question interrupted before the CLI took it in, then asked again
     word for word: the live copy is answered, the dead copy's answer is
     filed, never spoken as news
 plus: background tasks are tracked, deduplicated, and name their turn

Run:  .venv/Scripts/python tests/test_pipe_reader.py
"""
import ast
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakes  # noqa: E402
from fakes import (FakeClient, NOTIF, assistant, check_factory, echo,  # noqa
                   message_start, result, task_done, task_started, text)

import backtalk.brain as brain_mod  # noqa: E402
from backtalk.config import CFG  # noqa: E402

brain_mod.ClaudeSDKClient = FakeClient
UNSPOKEN = fakes.Unspoken()
brain_mod.signals.unspoken = UNSPOKEN

failures: list = []
check = check_factory(failures)


def reset_fakes():
    FakeClient.instances.clear()
    FakeClient.force_no_replay = False
    FakeClient.on_query = None
    FakeClient.on_interrupt = None
    fakes.EVENTS.clear()
    UNSPOKEN.records.clear()


async def settle(c=None, rounds=5):
    """Let the reader catch up with everything pushed so far."""
    for _ in range(2000):
        if c is None or c.frames.empty():
            break
        await asyncio.sleep(0)
    for _ in range(rounds):
        await asyncio.sleep(0)


def events(b):
    out = []
    while not b.events.empty():
        out.append(b.events.get_nowait())
    return out


def bg_sentences(evs):
    return [e.text for e in evs if e.kind == "sentence"]


def answering(answer, origin=None):
    """on_query hook: echo the prompt, stream an answer, end the turn."""
    def hook(cl, prompt):
        cl.consume(prompt)
        cl.push(message_start(), text(answer), result(origin=origin))
    return hook


async def ask(b, q):
    return [s async for s in b.ask_stream(q)]


async def warm_brain():
    b = brain_mod.WarmBrain(model="claude-test")
    await b.start()
    c = FakeClient.instances[-1]
    FakeClient.on_query = answering("Ready.")
    got = await ask(b, "Warmup ping - reply with the single word: ready")
    assert got == ["Ready."], got
    events(b)
    return b, c


# ---- 1 -------------------------------------------------------------------
async def t_regression():
    reset_fakes()
    b, c = await warm_brain()
    words = " ".join(["The app side is committed and pushed."] * 40)
    bg = [message_start()] + text(words)
    check("the background turn is at least 250 frames", len(bg) >= 250)
    c.push(bg, result(origin=NOTIF))
    FakeClient.on_query = answering("The build is on your S23. All good.")
    got = await ask(b, "Everything okay?")
    check("1. the question gets ITS answer, not the stale one",
          got == ["The build is on your S23.", "All good."])
    evs = events(b)
    bgs = bg_sentences(evs)
    check("1. the background text reached the background queue",
          len(bgs) == 40 and bgs[0] == "The app side is committed and pushed.")
    check("1. none of the answer leaked into the background queue",
          not any("S23" in s for s in bgs))
    check("1. the background turn opened and closed",
          [e.kind for e in evs if e.kind in ("turn_open", "turn_close")]
          == ["turn_open", "turn_close"])
    await b.stop()


# ---- 2 -------------------------------------------------------------------
def t_source_check():
    root = Path(__file__).resolve().parent.parent / "backtalk"
    offenders = []
    for py in sorted(root.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))

        def walk(node, fn):
            for child in ast.iter_child_nodes(node):
                name = fn
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = child.name
                if isinstance(child, ast.Attribute) and child.attr in (
                        "receive_messages", "receive_response"):
                    if fn != "_read_pipe":
                        offenders.append(f"{py.name}:{child.lineno} in {fn}")
                walk(child, name)
        walk(tree, None)
    check("2. only _read_pipe reads the pipe (source check)",
          offenders == [])
    if offenders:
        print("     ", offenders)


# ---- 3 -------------------------------------------------------------------
async def t_folded():
    reset_fakes()
    b, c = await warm_brain()
    c.push(message_start(), text("Bg one."))
    await settle(c)

    def hook(cl, prompt):
        cl.push(text("Bg two."))
        cl.consume(prompt)
        cl.push(text("Fg answer here."), result(origin=NOTIF))
    FakeClient.on_query = hook
    got = await ask(b, "What's left on Beepies?")
    evs = events(b)
    check("3. text after the echo is the answer", got == ["Fg answer here."])
    check("3. text before the echo is background",
          bg_sentences(evs) == ["Bg one.", "Bg two."])
    check("3. the background half closed as 'folded'",
          [e.reason for e in evs if e.kind == "turn_close"] == ["folded"])
    await b.stop()


# ---- 4 -------------------------------------------------------------------
async def t_no_echo():
    reset_fakes()
    FakeClient.force_no_replay = True
    old_hold = CFG.get("fg_hold_s")
    CFG["fg_hold_s"] = 0.3
    try:
        b = brain_mod.WarmBrain(model="claude-test")
        await b.start()
        c = FakeClient.instances[-1]
        FakeClient.on_query = (lambda cl, p: cl.push(
            message_start(), text("Ready."), result()))
        check("4. with no echo, the first answer still pairs by order",
              await ask(b, "Warmup ping") == ["Ready."])

        # a background turn is open when the question comes
        c.push(message_start(), text("Bg a."))
        await settle(c)
        loop = asyncio.get_running_loop()

        async def later():
            await asyncio.sleep(0.1)
            c.push(text("Bg b."))
        asyncio.create_task(later())
        FakeClient.on_query = (lambda cl, p: cl.push(
            text("Mixed c."), result(origin=NOTIF)))
        t0 = loop.time()
        got = await ask(b, "Status?")
        waited = c.sent_at[-1] - t0
        evs = events(b)
        check("4. the question was held for fg_hold_s before sending",
              0.28 <= waited < 1.0)
        check("4. after the hold the open turn counts as the answer",
              got == ["Mixed c."])
        check("4. what came before is background, nothing duplicated",
              bg_sentences(evs) == ["Bg a.", "Bg b."])

        # ...and if the background turn ends inside the hold, the
        # question goes out at once and the NEXT turn is the answer
        c.push(message_start(), text("Bg x."))
        await settle(c)

        async def ends():
            await asyncio.sleep(0.1)
            c.push(result(origin=NOTIF))
        asyncio.create_task(ends())
        FakeClient.on_query = (lambda cl, p: cl.push(
            message_start(), text("Answer y."), result()))
        t0 = loop.time()
        got = await ask(b, "And now?")
        waited = c.sent_at[-1] - t0
        evs = events(b)
        check("4. a turn that ends inside the hold releases the question",
              waited < 0.28)
        check("4. ...and the next turn is the answer", got == ["Answer y."])
        check("4. ...and the background text stayed background",
              bg_sentences(evs) == ["Bg x."])
        await b.stop()
    finally:
        CFG["fg_hold_s"] = old_hold


# ---- 5 -------------------------------------------------------------------
async def t_slow_rate_pull():
    reset_fakes()
    old = CFG.get("show_usage")
    CFG["show_usage"] = True
    try:
        b = brain_mod.WarmBrain(model="claude-test")
        await b.start()
        c = FakeClient.instances[-1]
        c._query.delay = 5.0
        FakeClient.on_query = answering("Ready.")
        await ask(b, "Warmup ping")       # its result spawns the 5 s pull
        before = c.iterated
        bg = [message_start()] + text(" ".join(["Word."] * 296))
        c.push(bg, result(origin=NOTIF))
        n = len(bg) + 1
        t0 = time.monotonic()
        while c.iterated - before < n and time.monotonic() - t0 < 2:
            await asyncio.sleep(0.01)
        took = time.monotonic() - t0
        check("5. the rate-limit pull was started", c._query.calls >= 1)
        check(f"5. all {n} frames read while it sleeps (took {took:.2f}s)",
              c.iterated - before == n and took < 1.0)
        await b.stop()
    finally:
        CFG["show_usage"] = old


# ---- 6 -------------------------------------------------------------------
async def t_reset_turn():
    reset_fakes()
    b, c = await warm_brain()

    def long_answer(cl, prompt):
        cl.consume(prompt)
        cl.push(message_start(), text("Long answer one."))
    FakeClient.on_query = long_answer
    first = asyncio.Event()

    async def consumer():
        async for _ in b.ask_stream("Tell me everything."):
            first.set()
    task = asyncio.create_task(consumer())
    await asyncio.wait_for(first.wait(), 2)
    task.cancel()                          # the person interrupted
    try:
        await task
    except asyncio.CancelledError:
        pass
    FakeClient.on_interrupt = (lambda cl: cl.push(text("Leftover bit."),
                                                  result()))
    n_clients = len(FakeClient.instances)
    await b.reset_turn(timeout=2)
    check("6. reset_turn interrupted the open turn", c.interrupts == 1)
    check("6. ...and it closed with no rebuild",
          b._open is None and len(FakeClient.instances) == n_clients)
    check("6. text nobody heard went to the file as 'interrupted'",
          any(r["reason"] == "interrupted" and "Leftover bit." in r["text"]
              for r in UNSPOKEN.records))

    # now a turn that never closes: rebuild, old reader first
    FakeClient.on_query = long_answer
    first.clear()
    task = asyncio.create_task(consumer())
    await asyncio.wait_for(first.wait(), 2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    FakeClient.on_interrupt = None
    fakes.EVENTS.clear()
    await b.reset_turn(timeout=0.3)
    await settle()                     # let the new reader start
    new = FakeClient.instances[-1]
    check("6. a turn that never closes gets the session rebuilt",
          new is not c and b._client is new)
    ev = fakes.EVENTS
    try:
        ok = ev.index(("iter_close", c.n)) < ev.index(("iter_open", new.n))
    except ValueError:
        ok = False
    check("6. the old reader was gone before the new one started", ok)
    if not ok:
        print("     ", ev, c.n, new.n)
    check("6. the old client was disconnected", not c.connected)
    FakeClient.on_query = answering("Fresh and working.")
    check("6. the rebuilt brain answers",
          await ask(b, "Hello?") == ["Fresh and working."])
    await b.stop()


# ---- 7 -------------------------------------------------------------------
async def t_subagents():
    reset_fakes()
    b, c = await warm_brain()
    c.push(text("Step 5 - stage only my two files.", parent="toolu_1"),
           assistant("Sub narration.", tool=("Read", {"file_path": "x"}),
                     parent="toolu_1"))
    await settle(c)
    check("7. sub-agent frames open no turn", b._open is None)
    check("7. ...and reach no queue", events(b) == [])

    def hook(cl, prompt):
        cl.consume(prompt)
        cl.push(message_start(), text("Sub says hi.", parent="toolu_9"),
                text("Main answer."), result())
    FakeClient.on_query = hook
    check("7. inside our turn they are never yielded",
          await ask(b, "Go.") == ["Main answer."])
    await b.stop()


# ---- 8 -------------------------------------------------------------------
async def t_reader_dies():
    reset_fakes()
    b, c = await warm_brain()
    c.push(message_start(), text("Half a background thought."))
    await settle(c)
    c.die()
    await settle(c)
    evs = events(b)
    check("8. the brain is marked lost", b.lost is True)
    check("8. the open turn closed as brain_lost",
          any(e.kind == "turn_close" and e.reason == "brain_lost"
              for e in evs))
    check("8. the floor is told (reset, brain_lost)",
          any(e.kind == "reset" and e.reason == "brain_lost" for e in evs))
    FakeClient.on_query = answering("Back again.")
    got = await ask(b, "Are you there?")
    check("8. the next question rebuilds and is answered",
          got == ["Back again."] and FakeClient.instances[-1] is not c
          and b.lost is False)
    await b.stop()


# ---- 9 -------------------------------------------------------------------
async def t_commands():
    reset_fakes()
    b, c = await warm_brain()
    FakeClient.on_query = (lambda cl, p: cl.push(
        assistant("Set effort level to low"), result()))
    check("9. a slash command with no echo still gets its reply",
          await b.command("/effort low") == "Set effort level to low")

    def wrapped(cl, p):
        cl.push(echo("<command-name>/model</command-name>"
                     "<command-args>claude-x</command-args>"),
                assistant("Set model to claude-x"), result())
    FakeClient.on_query = wrapped
    check("9. a slash command echoed in the CLI's wrapper pairs too",
          await b.command("/model claude-x") == "Set model to claude-x")
    FakeClient.on_query = answering("Still me.")
    check("9. and questions still pair after commands",
          await ask(b, "Who are you?") == ["Still me."])
    await b.stop()


# ---- 10 ------------------------------------------------------------------
async def t_missed_echo():
    reset_fakes()
    b, c = await warm_brain()
    # echo is proven on this connection; now one never matches
    FakeClient.on_query = (lambda cl, p: cl.push(
        echo("something the CLI rewrote entirely"),
        message_start(), text("An answer."), result(origin=None)))
    try:
        got = await asyncio.wait_for(ask(b, "Odd one?"), 2)
        ok = True
    except asyncio.TimeoutError:
        got, ok = None, False
    check("10. a missed echo never hangs the question", ok)
    check("10. ...the text went out as background, not lost",
          bg_sentences(events(b)) == ["An answer."] and got == [])
    await b.stop()


# ---- 11 ------------------------------------------------------------------
async def t_asked_again():
    """A question interrupted before the CLI took it in, then asked again
    word for word: the live copy must get an answer, not hang."""
    reset_fakes()
    b, c = await warm_brain()
    c.push(message_start(), text("A long background report."))
    await settle(c)
    FakeClient.on_query = None            # the CLI queues it, no echo yet
    first = asyncio.create_task(ask(b, "What time is it?"))
    await settle()
    first.cancel()                        # the person moved on
    try:
        await first
    except asyncio.CancelledError:
        pass

    def hook(cl, p):
        # the background turn ends, then the CLI takes BOTH copies in
        cl.push(result(origin=NOTIF))
        cl.consume(p)
        cl.push(message_start(), text("It's noon."), result())
        cl.consume(p)
        cl.push(message_start(), text("Still noon."), result())
    FakeClient.on_query = hook
    try:
        got = await asyncio.wait_for(ask(b, "What time is it?"), 2)
    except asyncio.TimeoutError:
        got = None
    check("11. the question asked again is answered, not hung",
          got == ["It's noon."])
    await settle(c)
    check("11. the dead copy's answer went to the file, not the air",
          any("Still noon." in r["text"] for r in UNSPOKEN.records))
    await b.stop()


# ---- tasks ---------------------------------------------------------------
async def t_tasks():
    reset_fakes()
    b, c = await warm_brain()
    c.push(task_started("t1", "Build the APK"))
    await settle(c)
    check("tasks: one in flight", b.tasks_in_flight == 1)
    c.push(task_done("t1"), task_done("t1"),     # reported twice
           message_start(), text("It built clean."), result(origin=NOTIF))
    await settle(c)
    evs = events(b)
    opens = [e for e in evs if e.kind == "turn_open"]
    check("tasks: none in flight after it finished", b.tasks_in_flight == 0)
    check("tasks: the woken turn knows what woke it",
          len(opens) == 1 and opens[0].info
          and opens[0].info["description"] == "Build the APK")
    check("tasks: every job carries one short label (log, floor, face)",
          opens and opens[0].info.get("label") == "Build the APK")
    check("tasks: a task reported twice is announced once",
          len([e for e in evs if e.kind == "task_done"]) == 1)
    await b.stop()


def main():
    t_source_check()
    for t in (t_regression, t_folded, t_no_echo, t_slow_rate_pull,
              t_reset_turn, t_subagents, t_reader_dies, t_commands,
              t_missed_echo, t_asked_again, t_tasks):
        try:
            asyncio.run(asyncio.wait_for(t(), 30))
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"{t.__name__} ran without raising ({e!r})", False)
    check("2. the fake never saw two iterators open at once",
          fakes.VIOLATIONS == [])
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
