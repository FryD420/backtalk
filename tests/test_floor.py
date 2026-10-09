# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The floor: who gets the speaker during turns. No audio.

Why this exists. Background news (a build agent finished, a test job
passed) used to be thrown away unspoken, or worse, spoken as the answer
to a different question. The floor (floor.py) gives it a place: spoken
when it lands, slipped into a reply at a chunk boundary, never over the
person, and written to .voice_unspoken whenever it is not spoken.

A FakeMouth records each chunk as it is enqueued and is advanced by
hand, so "what was heard, in what order" is exact.

Asserts (spec section 7, test_floor):
  1. idle news: the idle line, then the news
  2. interrupt: 6 answer sentences, 2 played, news lands -> the one
     playing, the interrupt line, the news, the resume line, then the
     rest of the answer, each sentence exactly once
  3. key held: nothing is fed; released: the held news plays
  4. permission pending: news held, the gate's question goes first
     (and a background job asking says so)
  5. a press during an interrupt: shut_up, and the file has the answer's
     rest and the news' rest with the right reasons and `partial`
  6. news folded into OUR turn (saw_news): no template at all
  7. a background turn with no text: no opening line, a no_text record
  8. cap and backlog: file records plus one rest_on_screen
  9. one-sentence chunks while a task is in flight, breaths when none
 10. while_you_talked comes after the answer
 plus: "off" writes instead of speaking, "idle" never cuts in, news
 held past background_hold_max_s is written as too_old

Run:  .venv/Scripts/python tests/test_floor.py
"""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakes  # noqa: E402
from fakes import (FakeClient, FakeMouth, NOTIF, Unspoken,  # noqa: E402
                   check_factory, message_start, notif_user, result, text)

from backtalk.brain import BgEvent  # noqa: E402
from backtalk.floor import (BG_SPEAK, DEFAULT_LINES, IDLE,  # noqa: E402
                            INTERRUPTING, Chunker, Floor, describe_task,
                            fill_line)
from backtalk.jobs import job_label  # noqa: E402

failures: list = []
check = check_factory(failures)

APK = {"id": "t1", "description": "Build the APK", "type": "local_agent",
       "status": "completed"}
TESTS = {"id": "t2", "description": "run the test suite",
         "type": "local_bash", "status": "completed"}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Rig:
    """A floor wired to fakes: a mouth, a brain stub, a clock, a key."""

    def __init__(self, mode="interrupt", in_flight=0, **cfg):
        self.mouth = FakeMouth()
        self.brain = types.SimpleNamespace(tasks_in_flight=in_flight,
                                           open_origin=None,
                                           events=asyncio.Queue())
        self.user = False
        self.perm = False
        self.clock = Clock()
        self.file = Unspoken()
        conf = {"background_speech": mode}
        conf.update(cfg)
        self.floor = Floor(self.mouth, brain=self.brain,
                           user_active=lambda: self.user,
                           perm_pending=lambda: self.perm,
                           clock=self.clock, cfg=conf, unspoken=self.file,
                           logf=lambda s: None)
        self._tid = 100

    # background turns
    def news(self, sentences, info=APK, close=True):
        self._tid += 1
        tid = self._tid
        f = self.floor
        f.handle_event(BgEvent("turn_open", tid, info=info))
        for s in sentences:
            f.handle_event(BgEvent("sentence", tid, text=s))
        if close:
            f.handle_event(BgEvent("turn_close", tid, info=info,
                                   reason="result"))
        return tid

    def more(self, tid, sentences=(), close=False, info=APK):
        for s in sentences:
            self.floor.handle_event(BgEvent("sentence", tid, text=s))
        if close:
            self.floor.handle_event(BgEvent("turn_close", tid, info=info,
                                            reason="result"))

    # an answer, as speak_reply drives it
    def answer(self, sentences, done=True):
        f = self.floor
        tok = f.fg_begin()
        ch = Chunker(lambda t, d: f.fg_chunk(t, d, tok),
                     single=f.single_sentence)
        for s in sentences:
            ch.feed(s)
        if done:
            ch.flush()
            f.fg_done(tok)
        return tok, ch

    def play(self, n=None):
        """Advance playback: finish the playing chunk, let the floor
        refill, repeat (n times, or until silent)."""
        i = 0
        while n is None or i < n:
            self.floor.pump()
            if not self.mouth.queue:
                break
            self.mouth.finish()
            self.floor.pump()
            i += 1
        return self.mouth.heard()


S = [f"Sentence {w}." for w in ("one", "two", "three", "four", "five",
                                "six")]


def t1_idle_news():
    r = Rig()
    r.news(["It built clean.", "It's on your S23."])
    heard = r.play()
    check("1. idle news: the idle line, then the news",
          heard == ["Heads up. The agent building the APK just finished.",
                    "It built clean.", "It's on your S23."])
    check("1. back to idle afterwards", r.floor.state == IDLE)


def t2_interrupt():
    r = Rig(in_flight=1)                 # one sentence per chunk
    r.answer(S, done=False)
    check("2. at most two chunks outstanding", r.mouth.outstanding == 2)
    r.play(2)                            # two sentences played
    check("2. two played, the third is playing",
          r.mouth.played == S[:2] and r.mouth.queue[0].text == S[2])
    r.news(["It built clean."])
    check("2. the floor is interrupting", r.floor.state == INTERRUPTING)
    check("2. yield_floor was used, the playing chunk left alone",
          r.mouth.yields == 1 and r.mouth.queue[0].text == S[2])
    r.floor.fg_done(r.floor._fg_token)
    heard = r.play()
    want = [S[0], S[1], S[2],
            "Hold on, we're being interrupted. The agent building the APK "
            "just finished.",
            "It built clean.",
            "Right, back to what I was saying.",
            S[3], S[4], S[5]]
    check("2. spoken order: current, interrupt, news, resume, the rest",
          heard == want)
    check("2. every answer sentence spoken exactly once",
          all(heard.count(s) == 1 for s in S))
    check("2. nothing went to the file", r.file.records == [])


def t3_key_held():
    r = Rig()
    r.user = True
    r.news(["All eleven pass."], info=TESTS)
    r.floor.pump()
    check("3. key held: nothing is fed", r.mouth.enqueued == [])
    r.user = False
    heard = r.play()
    check("3. released: the held news plays",
          heard == ["Heads up. The job running the test suite just finished.",
                    "All eleven pass."])


def t4_permission():
    r = Rig()
    r.answer(S[:3], done=False)          # s1 playing, s2+s3 queued
    r.floor.prompt("Permission check. I want to run a git command. "
                   "Yes, no, or details?", from_bg=False)
    r.perm = True
    r.news(["It built clean."])          # lands while the question waits
    heard = r.play()
    check("4. the question goes next, the answer's rest waits",
          heard == [S[0], "Permission check.",
                    "I want to run a git command.", "Yes, no, or details?"])
    check("4. the news is held while the question is pending",
          not any("built clean" in h for h in heard))
    r.perm = False                       # answered
    r.floor.fg_done(r.floor._fg_token)
    heard = r.play()
    # the answer still has text left, so in interrupt mode the held news
    # cuts in at that boundary and the answer resumes after it
    check("4. once answered, the held news plays and the answer resumes",
          heard[4:] == ["Hold on, we're being interrupted. The "
                        "agent building the APK just finished.",
                        "It built clean.",
                        "Right, back to what I was saying.",
                        S[1] + " " + S[2]])

    r = Rig()
    r.brain.open_origin = "bg"
    r.floor.prompt("Permission check. I want to edit a file. Yes, no, or "
                   "details?")
    check("4. a background job asking says so first",
          r.play()[0] == "A background job needs a yes or no.")


def t5_press_during_interrupt():
    r = Rig(in_flight=1)
    r.answer(S, done=False)
    r.play(2)
    tid = r.news(["It built clean.", "It's on your S23."], close=False)
    r.play(2)          # S3 and the interrupt line played: news is playing
    check("5. the news is playing",
          r.mouth.queue and r.mouth.queue[0].text == "It built clean.")
    r.floor.silence("interrupted")
    check("5. shut_up was called", r.mouth.shut_ups == 1)
    recs = r.file.records
    fg = [x for x in recs if x["origin"] == "fg"]
    bg = [x for x in recs if x["origin"] == "bg"]
    check("5. the answer's rest is filed as interrupted",
          len(fg) == 1 and fg[0]["reason"] == "interrupted"
          and fg[0]["text"] == " ".join(S[3:]) and not fg[0]["partial"])
    check("5. the news chunk cut mid-play is filed partial, silenced",
          any(x["reason"] == "silenced" and x["partial"]
              and x["text"] == "It built clean." for x in bg))
    check("5. the news not yet spoken is filed silenced",
          any(x["reason"] == "silenced" and not x["partial"]
              and x["text"] == "It's on your S23." for x in bg))
    r.more(tid, ["One more thing."], close=True)
    check("5. what the background turn says afterwards is filed too",
          r.file.records[-1]["reason"] == "silenced"
          and r.file.records[-1]["text"] == "One more thing.")
    check("5. and nothing more is spoken", r.play() == r.mouth.heard()
          and r.mouth.queue == [])


async def _t6():
    """Real brain + fake CLI: a notification folded into our own turn."""
    import backtalk.brain as brain_mod
    brain_mod.ClaudeSDKClient = FakeClient
    FakeClient.instances.clear()
    seen = {}
    b = brain_mod.WarmBrain(model="claude-test")
    real_close = b._close

    def spy(t, reason, result=None):
        if t.origin == "fg":
            seen["saw_news"] = t.saw_news
        return real_close(t, reason, result)
    b._close = spy
    await b.start()

    def hook(cl, p):
        cl.consume(p)
        cl.push(message_start(), text("On it."), notif_user(),
                text("Hold on, the build agent just finished, all clean. "
                     "Anyway, three things left."), result())
    FakeClient.on_query = hook
    r = Rig()
    r.floor.brain = b
    tok = r.floor.fg_begin()
    ch = Chunker(lambda t, d: r.floor.fg_chunk(t, d, tok))
    async for s in b.ask_stream("What's left?"):
        ch.feed(s)
    ch.flush()
    r.floor.fg_done(tok)
    while not b.events.empty():
        r.floor.handle_event(b.events.get_nowait())
    heard = r.play()
    await b.stop()
    FakeClient.on_query = None
    return seen, heard


def t6_saw_news():
    seen, heard = asyncio.run(_t6())
    check("6. the brain marked the fg turn saw_news",
          seen.get("saw_news") is True)
    check("6. no template line around model-told news",
          not any(h.startswith(("Hold on, we're", "Heads up", "Right, back"))
                  for h in heard) and heard[0] == "On it.")


def t7_no_text():
    r = Rig()
    r.news([])
    check("7. a silent background turn gets no opening line",
          r.play() == [])
    check("7. ...and a no_text record that names it",
          r.file.records and r.file.records[0]["reason"] == "no_text"
          and r.file.records[0]["what"] == "Build the APK")


def t8_cap_and_backlog():
    r = Rig(background_max_sentences=4)
    r.news([f"News {i}." for i in range(1, 7)])
    heard = r.play()
    check("8. four news sentences spoken, then rest_on_screen once",
          heard[1:] == ["News 1.", "News 2. News 3.", "News 4.",
                        "The rest is on the screen."])
    check("8. the rest is filed as capped",
          [x for x in r.file.records if x["reason"] == "capped"][0]["text"]
          == "News 5. News 6.")

    r = Rig(background_backlog=3)
    r.user = True                         # hold everything
    for i in range(5):
        r.news([f"Job {i} done."], info={"description": f"job {i}",
                                         "type": "local_bash",
                                         "status": "completed"})
    back = [x for x in r.file.records if x["reason"] == "backlog"]
    check("8. backlog: the two oldest of five are filed",
          [x["text"] for x in back] == ["Job 0 done.", "Job 1 done."])
    r.user = False
    heard = r.play()
    check("8. the three newest are spoken, chained with 'another'",
          heard == ["Heads up. Job 2 just finished.", "Job 2 done.",
                    "And another one.", "Job 3 done.",
                    "And another one.", "Job 4 done."])


def t9_chunk_sizes():
    r = Rig(in_flight=1)
    r.answer(S[:5])
    check("9. a task in flight: one sentence per chunk",
          r.play() == S[:5])
    r = Rig(in_flight=0)
    r.answer(S[:5])
    check("9. none in flight: first alone, then two-sentence breaths",
          r.play() == [S[0], f"{S[1]} {S[2]}", f"{S[3]} {S[4]}"])


def t10_while_you_talked():
    r = Rig()
    r.user = True                         # key down, news lands
    r.news(["All eleven pass."], info=TESTS)
    r.user = False                        # released: the question goes out
    r.answer(["Here's the answer.", "That's all."])
    heard = r.play()
    check("10. the answer first, then while_you_talked and the news",
          heard == ["Here's the answer.", "That's all.",
                    "Also, while you were talking: the job running the "
                    "test suite finished.", "All eleven pass."])


def t_off_idle_too_old():
    r = Rig(mode="off")
    r.news(["It built clean."])
    check("off: nothing is spoken", r.play() == [])
    check("off: the text is filed as speech_off",
          r.file.records[0]["reason"] == "speech_off"
          and r.file.records[0]["text"] == "It built clean.")

    r = Rig(mode="idle")
    r.answer(S[:5], done=False)
    r.play(1)
    r.news(["It built clean."])
    check("idle: no interrupt while the answer plays",
          r.floor.state != INTERRUPTING and r.mouth.yields == 0)
    r.floor.fg_done(r.floor._fg_token)
    heard = r.play()
    check("idle: the news follows the answer with the idle line",
          heard[-2:] == ["Heads up. The agent building the APK just finished.",
                         "It built clean."] and heard.count(S[3] + " "
                                                            + S[4]) == 1)

    r = Rig()
    r.user = True
    r.news(["Old news."])
    r.clock.t += 601
    r.user = False
    check("too_old: news held past the limit is not spoken", r.play() == [])
    check("too_old: ...it is filed",
          r.file.records and r.file.records[0]["reason"] == "too_old")


def t_park():
    """News that goes quiet mid-interrupt (its turn is in a long tool
    call) must not hold the rest of the answer hostage."""
    r = Rig(in_flight=1)
    r.answer(S[:4], done=True)
    r.play(1)
    tid = r.news(["Starting the deploy."], close=False)
    r.play(3)              # S2, the interrupt line, the news sentence
    check("park: the news turn is quiet and the mouth has run dry",
          r.mouth.queue == [] and r.floor.state == INTERRUPTING)
    r.floor.pump()
    check("park: not parked before it has been quiet a second",
          r.floor.state == INTERRUPTING and r.mouth.queue == [])
    r.clock.t += 1.5
    heard = r.play()
    check("park: then the answer resumes, with the resume line",
          heard[-3:] == ["Right, back to what I was saying.", S[2], S[3]])
    r.more(tid, ["Deploy finished."], close=True)
    heard = r.play()
    check("park: the rest of the news is told after the answer",
          heard[-2:] == ["Heads up. The agent building the APK just finished.",
                         "Deploy finished."])
    check("park: every answer sentence exactly once",
          all(heard.count(s) == 1 for s in S[:4]))

    r = Rig()
    r.news(["One.", "Two."], close=False)   # "Two." waits for a pair
    heard = r.play()
    check("park: a held second sentence is said when the mouth runs dry",
          heard[-1] == "Two.")


def t_speak_reply():
    """main.speak_reply drives the floor: chunks in, fg_done at the end,
    and the cancel path still interrupts the brain."""
    import backtalk.main as main
    main.signals.static_stop = lambda: None
    main.signals.set_state = lambda s: None

    class Brain:
        interrupts = 0
        tasks_in_flight = 0
        open_origin = None

        def __init__(self, sentences, hang=False):
            self.sentences, self.hang = sentences, hang

        async def ask_stream(self, q):
            for s in self.sentences:
                yield s
            if self.hang:
                await asyncio.sleep(30)

        async def interrupt(self):
            Brain.interrupts += 1

    async def go():
        r = Rig()
        r.floor.brain = Brain(["Hi there.", "Code: ```x = 1``` done.",
                               "<<glow>>Last one."])
        await main.speak_reply(r.floor.brain, r.floor, "q")
        heard = r.play()
        check("speak_reply: first alone, fences and directions lifted",
              heard == ["Hi there.", "Code: done. Last one."])
        check("speak_reply: back to idle", r.floor.state == IDLE)

        r = Rig()
        b = Brain(["Before the cut."], hang=True)
        r.floor.brain = b
        task = asyncio.create_task(main.speak_reply(b, r.floor, "q"))
        await asyncio.sleep(0.05)
        task.cancel()
        r.floor.silence("interrupted")
        try:
            await task
        except asyncio.CancelledError:
            pass
        check("speak_reply: a cancel still interrupts the brain",
              Brain.interrupts == 1)
        check("speak_reply: the cut reply is filed, nothing left queued",
              r.file.records and r.file.records[0]["partial"]
              and r.mouth.queue == [] and r.floor.state == IDLE)
    asyncio.run(go())


def t_shutdown():
    r = Rig()
    r.user = True
    r.news(["Queued news."])
    r.answer(["Never said."], done=False)
    r.floor.shutdown()
    reasons = sorted(x["reason"] for x in r.file.records)
    check("shutdown: held and queued text is filed as shutdown",
          reasons == ["shutdown", "shutdown"])


def t_describe():
    """Job names read naturally out loud (backtalk/jobs.py): no hyphen
    chains, no raw shell commands, one short label for screens."""
    check("describe: an agent",
          describe_task(APK) == "the agent building the APK")
    check("describe: a job", describe_task(TESTS)
          == "the job running the test suite")
    check("describe: unknown is Something", describe_task(None) == "Something")
    check("describe: no description is Something",
          describe_task({"description": "", "type": None}) == "Something")
    check("label: the agent's own title, sentence case",
          job_label(APK) == "Build the APK"
          and job_label(TESTS) == "Run the test suite")
    check("label: unknown is Something", job_label(None) == "Something")
    log = {"description": "Log state in daily note and back up",
           "type": "local_bash"}
    check("describe: verbs after 'and' stay parallel",
          describe_task(log)
          == "the job logging state in daily note and backing up")
    check("label: first clause only, paths dropped",
          job_label({"description": "Dry-run fetch all remotes per stack "
                                    "repo, show exit codes"})
          == "Dry-run fetch all remotes per stack repo"
          and job_label({"description": "`fix` E:/x/y.py and then run all "
                                        "of the many many tests"})
          == "Fix and then run all of the many")
    check("describe: a noun-phrase title is said as is",
          describe_task({"description": "Beepies build",
                         "type": "local_agent"}) == "Beepies build")
    npm = {"description": "cd /e/dev/beepies-spike-step2/server && npm ci "
                          "2>&1 | tail -3 && npm test 2>&1 | tail -12",
           "type": "local_bash"}
    check("raw command: project + what it did",
          job_label(npm) == "Beepies spike test run"
          and describe_task(npm) == "the Beepies spike test run")
    heredoc = {"description": "cd /e/dev/beepies-spike-step2/server && "
                              "python - <<'EOF'\nimport x\nEOF",
               "type": "local_bash"}
    check("raw command: a heredoc is one short line",
          job_label(heredoc) == "Beepies spike command")
    check("raw command: git push through -C",
          job_label({"description": "git -C /e/my-agent/jarvis-gui push "
                                    "origin main"}) == "Jarvis gui push")
    check("raw command: no path, still short",
          describe_task({"description": "sleep 20 && echo done"})
          == "the timer")
    check("filled line, start to end",
          fill_line(DEFAULT_LINES["idle"], dict(npm, status="completed"))
          == "Heads up. The Beepies spike test run just finished.")
    check("filled line: stopped reads 'got stopped'",
          fill_line(DEFAULT_LINES["interrupt"], dict(APK, status="killed"))
          == "Hold on, we're being interrupted. The agent building the "
             "APK just got stopped.")


def main():
    for t in (t1_idle_news, t2_interrupt, t3_key_held, t4_permission,
              t5_press_during_interrupt, t6_saw_news, t7_no_text,
              t8_cap_and_backlog, t9_chunk_sizes, t10_while_you_talked,
              t_off_idle_too_old, t_park, t_speak_reply, t_shutdown,
              t_describe):
        try:
            t()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"{t.__name__} ran without raising ({e!r})", False)
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
