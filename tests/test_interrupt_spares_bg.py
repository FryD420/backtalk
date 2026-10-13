# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Talking over a reply must never kill the background agents.

Why this exists. On 2026-10-13 three background agents died with
`stoppedByUser` the instant Joe pressed the talk key (or typed) while
Jarvis was busy: every takeover called the SDK's client.interrupt(),
and Claude Code 2.1.294 answers a bare interrupt by killing every
background AGENT in the session, whether a turn is streaming or not.
A gate run against the real CLI the same day proved the way out: a
second query() sent mid-turn is folded in at the next tool boundary and
the background agent finishes; client.interrupt() kills it at once.

So a takeover (key press, typed line, inbox line, open-mic barge-in)
now only silences the speaker locally and sends the words into the
open turn. The one real interrupt left is an explicit stop phrase
spoken (or typed) alone, and it asks first when agents are running.

Asserts:
  1. key press mid-reply -> 0 interrupts, the new words are sent
  2. typed / inbox line mid-reply -> 0 interrupts, sent
  3. open-mic barge-in mid-reply -> 0 interrupts, the playing chunk is
     cut and filed
  4. the old turn's leftover text goes to .voice_unspoken as
     "superseded", never to the speaker
  5. the next reply is the answer to the NEW words, and the one after
     that pairs too (no off-by-one)
  6. the old turn ends on its own before the new words are taken in:
     still filed as superseded, the new words still get their answer
  7. "stop" alone, our turn running, no agents -> 1 interrupt
  8. "stop" alone, no turn of ours running -> mute only, 0 interrupts
  9. "stop" with agents running -> asks out loud, 0 interrupts
 10. a second "stop" inside the window -> 1 interrupt; outside it, or
     after another utterance in between -> asks again, 0 interrupts
 11. "stop" inside a longer sentence, or a console verb starting with
     "stop" -> not a stop, 0 interrupts
 12. a press while a permission question waits: playback only, the
     turn and the question are left alone (unchanged behaviour)
 13. source: only brain.reset_turn calls client.interrupt(), and main
     reaches reset_turn only from the explicit stop

Run:  .venv/Scripts/python tests/test_interrupt_spares_bg.py
"""
import ast
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakes  # noqa: E402
from fakes import (FakeClient, FakeMouth, Unspoken, check_factory,  # noqa
                   message_start, result, task_done, task_started, text)

import backtalk.brain as brain_mod  # noqa: E402
import backtalk.main as main  # noqa: E402
from backtalk.floor import Floor  # noqa: E402

# no thinking sound and no bus files from a unit test
for _name in ("static_start", "static_stop", "turn_begin", "turn_end"):
    setattr(main.signals, _name, lambda *a, **k: None)
main.signals.set_state = lambda *a, **k: None
main.signals.activity = lambda *a, **k: None
brain_mod.ClaudeSDKClient = FakeClient
UNSPOKEN = Unspoken()
brain_mod.signals.unspoken = UNSPOKEN

failures: list = []
check = check_factory(failures)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def settle(c=None, rounds=8):
    for _ in range(2000):
        if c is None or c.frames.empty():
            break
        await asyncio.sleep(0)
    for _ in range(rounds):
        await asyncio.sleep(0)


async def until(cond, limit=400):
    for _ in range(limit):
        if cond():
            return True
        await asyncio.sleep(0.002)
    return cond()


def open_answer(*sentences):
    """on_query hook: echo the prompt, start talking, leave the turn OPEN
    (the agent is still working: a tool call is running)."""
    def hook(cl, prompt):
        cl.consume(prompt)
        cl.push(message_start(), text(" ".join(sentences)))
    return hook


def full_answer(*sentences):
    def hook(cl, prompt):
        cl.consume(prompt)
        cl.push(message_start(), text(" ".join(sentences)), result())
    return hook


async def console_never(verb):
    raise AssertionError(f"no console verb expected, got {verb!r}")


class Rig:
    async def start(self, agents=0):
        FakeClient.instances.clear()
        FakeClient.on_query = None
        FakeClient.on_interrupt = None
        UNSPOKEN.records.clear()
        main._PERM.update(fut=None, asked_at=0.0, hinted=True)
        self.mouth = FakeMouth()
        self.clock = Clock()
        self.brain = brain_mod.WarmBrain(model="claude-test")
        await self.brain.start()
        self.c = FakeClient.instances[-1]
        FakeClient.on_query = full_answer("Ready.")
        async for _ in self.brain.ask_stream("Warmup ping"):
            pass
        self.floor = Floor(self.mouth, brain=self.brain,
                           user_active=lambda: False,
                           perm_pending=lambda: (main._PERM["fut"] is not None
                                                 and not main._PERM["fut"].done()),
                           cfg={"background_speech": "off"},
                           unspoken=UNSPOKEN, logf=lambda s: None)
        self.turns = main.Turns(self.brain, self.floor, self.mouth,
                                clock=self.clock, logf=lambda s: None)
        for i in range(agents):
            self.c.push(task_started(f"a{i}", f"Background agent {i}"))
        await settle(self.c)
        while not self.brain.events.empty():
            self.brain.events.get_nowait()
        return self

    async def reply_open(self, q, *sentences):
        """Ask q; the agent starts answering and keeps working."""
        FakeClient.on_query = open_answer(*sentences)
        self.turns.ask(q)
        first = sentences[0]
        ok = await until(lambda: first in self.mouth.enqueued)
        assert ok, f"the reply never reached the mouth: {self.mouth.enqueued}"
        assert self.turns.busy

    async def answer_of(self):
        """Let the current reply finish, playing everything it says."""
        task = self.turns.speak_task
        if task is not None:
            await asyncio.wait_for(task, 2)
        while self.mouth.queue:
            self.mouth.finish()
            self.floor.pump()

    def superseded_text(self):
        return " ".join(r["text"] for r in UNSPOKEN.records
                        if r["origin"] == "fg"
                        and r["reason"] == "superseded")

    async def stop(self):
        await self.brain.stop()


# ---- 1, 4, 5 -------------------------------------------------------------
async def t_key_press():
    r = await Rig().start(agents=1)
    await r.reply_open("Tell me the long version.",
                       "First sentence of the old reply.",
                       "Second sentence of the old reply.")
    r.turns.press(perm_wait=False)
    check("1. the press muted the speaker", r.mouth.shut_ups >= 1)
    check("1. the press did not interrupt the CLI", r.c.interrupts == 0)
    # the old turn keeps working and talking while Joe is still speaking
    r.c.push(text("Old leftover sentence."))
    await settle(r.c)
    FakeClient.on_query = full_answer("New answer for the new words.")
    await r.turns.submit("What about the questions?", None, console_never)
    await r.answer_of()
    check("1. a key press mid-reply sends 0 interrupts", r.c.interrupts == 0)
    check("1. the new words went into the open turn",
          r.c.sent[-1] == "What about the questions?")
    check("4. the old leftover never reached the speaker",
          not any("Old leftover" in t for t in r.mouth.enqueued))
    check("4. the old leftover was filed as superseded",
          "Old leftover sentence." in r.superseded_text())
    check("5. the reply is the answer to the NEW words",
          r.mouth.played[-1:] == ["New answer for the new words."])
    check("background agent still counted as running",
          r.brain.tasks_in_flight == 1)
    # and the next question pairs with ITS answer
    FakeClient.on_query = full_answer("Third answer.")
    r.turns.ask("And the third thing?")
    await r.answer_of()
    check("5. the question after that pairs too (no off-by-one)",
          r.mouth.played[-1:] == ["Third answer."])
    await r.stop()


# ---- 2 -------------------------------------------------------------------
async def t_typed_and_inbox():
    for source in ("typed", "inbox"):
        r = await Rig().start(agents=1)
        await r.reply_open("Walk me through it.", "Old reply one.",
                           "Old reply two.")
        FakeClient.on_query = full_answer(f"Answer to the {source} line.")
        # typed and inbox lines both arrive through typed_q -> handle
        await r.turns.submit(f"A {source} line mid-reply", None,
                             console_never)
        await r.answer_of()
        check(f"2. a {source} line mid-reply sends 0 interrupts",
              r.c.interrupts == 0)
        check(f"2. the {source} line was sent",
              r.c.sent[-1] == f"A {source} line mid-reply")
        check(f"2. the {source} line got its own answer",
              r.mouth.played[-1:] == [f"Answer to the {source} line."])
        await r.stop()


# ---- 3 -------------------------------------------------------------------
async def t_barge_in():
    r = await Rig().start(agents=2)
    await r.reply_open("Read me the list.", "Item one is long.",
                       "Item two is longer.")
    playing = r.mouth.queue[0].text
    FakeClient.on_query = full_answer("Okay, switching.")
    # an open-mic utterance lands in handle() like a typed line, with no
    # press first: the reply is still playing
    await r.turns.submit("hold on, switch topics", None, console_never)
    await r.answer_of()
    check("3. a barge-in mid-reply sends 0 interrupts", r.c.interrupts == 0)
    check("3. the playing chunk was cut", playing in r.mouth.cut)
    check("3. the cut chunk is filed as superseded, partial",
          any(x["reason"] == "superseded" and x["partial"]
              and x["text"] == playing for x in UNSPOKEN.records))
    check("3. the barge-in got its answer",
          r.mouth.played[-1:] == ["Okay, switching."])
    await r.stop()


# ---- 6 -------------------------------------------------------------------
async def t_old_turn_ends_first():
    r = await Rig().start(agents=1)
    await r.reply_open("Explain the plan.", "Plan part one.",
                       "Plan part two.")
    r.turns.press(perm_wait=False)
    # the old turn finishes on its own while Joe is still talking
    r.c.push(text("The tail of the plan."), result())
    await settle(r.c)
    FakeClient.on_query = full_answer("Fresh answer.")
    await r.turns.submit("Something else entirely.", None, console_never)
    await r.answer_of()
    check("6. 0 interrupts", r.c.interrupts == 0)
    check("6. the tail is filed as superseded",
          "The tail of the plan." in r.superseded_text())
    check("6. the new words get their own answer",
          r.mouth.played[-1:] == ["Fresh answer."])
    await r.stop()


# ---- 7, 8 ----------------------------------------------------------------
async def t_stop_no_agents():
    r = await Rig().start(agents=0)
    await r.reply_open("Go on at length.", "Rambling one.", "Rambling two.")
    FakeClient.on_interrupt = lambda cl: cl.push(result())
    got = await r.turns.submit("Stop.", None, console_never)
    check("7. 'stop' with our turn running and no agents interrupts",
          r.c.interrupts == 1 and got == "interrupted")
    check("7. ...the turn closed, and nothing new was asked",
          r.brain.open_origin is None and r.c.sent[-1] == "Go on at length.")
    check("7. ...and the speaker is quiet", r.mouth.queue == [])

    # 8: no turn of ours is running (the reply already ended, only the
    # audio is left): an interrupt would only kill agents. Mute only.
    FakeClient.on_query = full_answer("Short one.", "Short two.")
    r.turns.ask("A short question.")
    await asyncio.wait_for(r.turns.speak_task, 2)
    before = r.c.interrupts
    got = await r.turns.submit("stop", None, console_never)
    check("8. 'stop' with no turn of ours running only mutes",
          r.c.interrupts == before and got == "muted")
    check("8. ...and the speaker is quiet", r.mouth.queue == [])
    await r.stop()


# ---- 9, 10 ---------------------------------------------------------------
async def t_stop_with_agents():
    r = await Rig().start(agents=2)
    await r.reply_open("Do the big thing.", "Working on it.",
                       "Still going.")
    FakeClient.on_interrupt = lambda cl: cl.push(result())
    got = await r.turns.submit("stop", None, console_never)
    check("9. 'stop' with agents running asks first",
          got == "asked" and r.c.interrupts == 0)
    check("9. ...out loud, naming how many",
          "That will stop 2 background jobs. Say stop again to confirm."
          in " ".join(r.mouth.enqueued))
    check("9. ...and the turn is still running",
          r.brain.open_origin == "fg")
    r.clock.t += 5
    got = await r.turns.submit("Stop that.", None, console_never)
    check("10. a second 'stop' inside the window interrupts",
          got == "interrupted" and r.c.interrupts == 1)
    await r.stop()

    # outside the window: asks again
    r = await Rig().start(agents=1)
    await r.reply_open("Another big job.", "Big one.", "Bigger.")
    got = await r.turns.submit("stop", None, console_never)
    r.clock.t += main.STOP_CONFIRM_S + 1
    got2 = await r.turns.submit("stop", None, console_never)
    check("10. a second 'stop' after the window asks again",
          got == "asked" and got2 == "asked" and r.c.interrupts == 0)
    check("10. ...singular when there is one job",
          "That will stop 1 background job. Say stop again to confirm."
          in " ".join(r.mouth.enqueued))
    await r.stop()

    # anything else said in between cancels the pending confirm
    r = await Rig().start(agents=1)
    await r.reply_open("Third big job.", "Busy.", "Busier.")
    await r.turns.submit("stop", None, console_never)
    FakeClient.on_query = open_answer("Answering the aside.", "More.")
    await r.turns.submit("what time is it", None, console_never)
    await until(lambda: "Answering the aside." in r.mouth.enqueued)
    got = await r.turns.submit("stop", None, console_never)
    check("10. another utterance between the stops resets the confirm",
          got == "asked" and r.c.interrupts == 0)
    await r.stop()


# ---- 11 ------------------------------------------------------------------
async def t_stop_in_sentence():
    r = await Rig().start(agents=0)
    await r.reply_open("Keep going.", "Going one.", "Going two.")
    FakeClient.on_query = full_answer("Sure, I'll leave the build.")
    got = await r.turns.submit("Stop the build and tell me the time",
                               None, console_never)
    await r.answer_of()
    check("11. 'stop' inside a longer sentence is not a stop",
          r.c.interrupts == 0 and got == "sent"
          and r.c.sent[-1] == "Stop the build and tell me the time")
    seen = []

    async def console(verb):
        seen.append(verb)
    got = await r.turns.submit("stop asking for permission", None, console)
    check("11. a console verb starting with 'stop' stays a console verb",
          seen == ["noask"] and r.c.interrupts == 0 and got == "console")
    for phrase in ("stop", "Stop.", "stop that", "stop it", "cancel that",
                   "never mind", "Nevermind."):
        check(f"11. {phrase!r} alone is a stop phrase",
              main.is_stop_phrase(phrase))
    for phrase in ("stop asking for permission", "don't stop",
                   "stop the build", "bus stop", "okay"):
        check(f"11. {phrase!r} is not a stop phrase",
              not main.is_stop_phrase(phrase))
    for phrase in main.STOP_PHRASES:
        check(f"11. stop phrase {phrase!r} collides with no console verb",
              main.console_match(phrase) is None)
    await r.stop()


# ---- 12 ------------------------------------------------------------------
async def t_press_during_permission():
    r = await Rig().start(agents=1)
    await r.reply_open("Change the note.", "On it.", "Writing now.")
    loop = asyncio.get_running_loop()
    main._PERM["fut"] = loop.create_future()
    task = r.turns.speak_task
    r.turns.press(perm_wait=True)
    await settle()
    check("12. a press during a permission question leaves the turn alive",
          not task.done() and r.c.interrupts == 0)
    check("12. ...and the question still waits for its answer",
          not main._PERM["fut"].done())
    main._PERM["fut"].set_result("no")
    main._PERM["fut"] = None
    r.c.push(result())
    await asyncio.wait_for(task, 2)
    await r.stop()


# ---- 13 ------------------------------------------------------------------
def t_source():
    def calls(path, attr):
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        out = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == attr):
                        out.append(node.name)
        return out
    brain_calls = calls(ROOT / "backtalk" / "brain.py", "interrupt")
    check("13. only brain.reset_turn calls client.interrupt()",
          brain_calls and set(brain_calls) == {"reset_turn"})
    check("13. WarmBrain has no interrupt() shortcut any more",
          not hasattr(brain_mod.WarmBrain, "interrupt"))
    main_int = calls(ROOT / "backtalk" / "main.py", "interrupt")
    check("13. main.py never calls .interrupt()", main_int == [])
    main_reset = calls(ROOT / "backtalk" / "main.py", "reset_turn")
    check("13. main reaches reset_turn only from the explicit stop",
          set(main_reset) == {"stop"})


def main_():
    t_source()
    for t in (t_key_press, t_typed_and_inbox, t_barge_in,
              t_old_turn_ends_first, t_stop_no_agents, t_stop_with_agents,
              t_stop_in_sentence, t_press_during_permission):
        try:
            asyncio.run(asyncio.wait_for(t(), 30))
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"{t.__name__} ran without raising ({e!r})", False)
    check("the fake never saw two iterators open at once",
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
    raise SystemExit(main_())
