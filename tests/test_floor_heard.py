# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The floor tells the dashboard which background news was HEARD.

Why this exists. A dashboard (jarvis-gui's agent roster) shows a
finished background agent green until its news has been heard, and its
"clear" button may only throw away news the person actually heard
(Joe, 2026-10-13). The floor is the one place that knows: it fed the
news to the mouth and watched every chunk play. So when a background
turn's news is spoken TO THE END, the floor records its task id
(signals.heard -> .voice_heard). Anything less is not heard.

Heard means all of: the turn closed, every news chunk it fed finished
playing, nothing was cut, nothing was capped, nothing was dropped, and
the turn carries a task id. Not heard: cut off by a key press, capped
("the rest is on the screen"), backlog, too_old, speech off, a turn
with no text, a rebuild, shutdown, and a turn with no task info.

Asserts:
  1. idle news spoken to the end: one heard record, the task id and label
  2. not before the last chunk has finished playing, nor before the
     turn closes; and recorded exactly once
  3. a key press mid-news (silence): nothing heard
  4. a key press while a permission question waits (silence_playback)
     cutting a news chunk: nothing heard
  5. capped news: nothing heard
  6. speech off / no text / too_old: nothing heard
  7. backlog: the dropped jobs are not heard, the spoken ones are
  8. interrupt mode: news told mid-answer, to the end, is heard
  9. parked news told after the answer is heard once it ends
 10. a turn with no task info (or no id) records nothing and never raises
 11. a heard callback that raises never breaks the floor

Run:  .venv/Scripts/python tests/test_floor_heard.py
"""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeMouth, Unspoken, check_factory  # noqa: E402

from backtalk.brain import BgEvent  # noqa: E402
from backtalk.floor import Chunker, Floor  # noqa: E402

failures: list = []
check = check_factory(failures)

APK = {"id": "a0038b2113e7f9544", "description": "Build the APK",
       "type": "local_agent", "status": "completed",
       "label": "Build the APK"}
S = [f"Sentence {w}." for w in ("one", "two", "three", "four")]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Heard:
    def __init__(self):
        self.records = []

    def __call__(self, task_id, label=None):
        self.records.append({"task_id": task_id, "label": label})


class Rig:
    def __init__(self, mode="idle", in_flight=0, heard=None, **cfg):
        self.mouth = FakeMouth()
        self.brain = types.SimpleNamespace(tasks_in_flight=in_flight,
                                           open_origin=None,
                                           events=asyncio.Queue())
        self.user = False
        self.perm = False
        self.clock = Clock()
        self.file = Unspoken()
        self.heard = Heard() if heard is None else heard
        conf = {"background_speech": mode}
        conf.update(cfg)
        self.floor = Floor(self.mouth, brain=self.brain,
                           user_active=lambda: self.user,
                           perm_pending=lambda: self.perm,
                           clock=self.clock, cfg=conf, unspoken=self.file,
                           heard=self.heard, logf=lambda s: None)
        self._tid = 100

    def news(self, sentences, info=APK, close=True):
        self._tid += 1
        tid = self._tid
        f = self.floor
        f.handle_event(BgEvent("turn_open", tid, info=info))
        for s in sentences:
            f.handle_event(BgEvent("sentence", tid, text=s))
        if close:
            self.close(tid, info)
        return tid

    def close(self, tid, info=APK):
        self.floor.handle_event(BgEvent("turn_close", tid, info=info,
                                        reason="result"))

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
        return tok

    def play(self, n=None):
        i = 0
        while n is None or i < n:
            self.floor.pump()
            if not self.mouth.queue:
                break
            self.mouth.finish()
            self.floor.pump()
            i += 1
        return self.mouth.heard()


def ids(r):
    return [x["task_id"] for x in r.heard.records]


def t1_spoken_to_the_end():
    r = Rig()
    r.news(["It built clean.", "It's on your S23."])
    r.play()
    check("1. news spoken to the end records one heard line",
          r.heard.records == [{"task_id": "a0038b2113e7f9544",
                               "label": "Build the APK"}])


def t2_timing_and_once():
    r = Rig()
    r.news(["It built clean.", "It's on your S23.", "All good."])
    r.play(2)                            # the idle line and one sentence
    check("2. not heard while news is still playing", ids(r) == [])
    r.play()
    check("2. heard once the last chunk finished", len(ids(r)) == 1)
    for _ in range(5):
        r.floor.pump()
    check("2. ...and recorded exactly once", len(ids(r)) == 1)

    r = Rig()
    tid = r.news(["It built clean."], close=False)
    r.play()
    check("2. all of it played but the turn is still open: not yet",
          ids(r) == [])
    r.close(tid)
    r.play()
    check("2. the turn closes with nothing more: heard", len(ids(r)) == 1)


def t3_key_press():
    r = Rig()
    r.news(["It built clean.", "It's on your S23.", "All good."])
    r.play(2)
    r.floor.silence("interrupted")
    r.play()
    check("3. a key press mid-news: nothing heard", ids(r) == [])

    r = Rig()
    tid = r.news(["It built clean."], close=False)
    r.play(1)                            # the idle line; the news plays
    r.floor.silence("superseded")
    r.close(tid)
    r.play()
    check("3. barge-in on the only sentence: nothing heard", ids(r) == [])


def t4_silence_playback():
    r = Rig()
    r.news(["It built clean.", "It's on your S23.", "All good."])
    r.play(1)                            # the idle line; news now playing
    r.floor.silence_playback()           # a press answering a question
    r.play()
    check("4. a news chunk cut by silence_playback: nothing heard",
          ids(r) == [])


def t5_capped():
    r = Rig(background_max_sentences=2)
    r.news([f"News {i}." for i in range(1, 5)])
    r.play()
    check("5. capped news (the rest on screen): nothing heard",
          ids(r) == [] and "capped" in r.file.reasons())


def t6_never_spoken():
    r = Rig(mode="off")
    r.news(["It built clean."])
    r.play()
    check("6. speech off: nothing heard", ids(r) == [])

    r = Rig()
    r.news([])
    r.play()
    check("6. a turn with no text: nothing heard", ids(r) == [])

    r = Rig()
    r.user = True
    r.news(["Old news."])
    r.clock.t += 601
    r.user = False
    r.play()
    check("6. too_old: nothing heard", ids(r) == [])


def t7_backlog():
    r = Rig(background_backlog=2)
    r.user = True
    for i in range(4):
        r.news([f"Job {i} done."], info={"id": f"t{i}",
                                         "description": f"job {i}",
                                         "type": "local_bash",
                                         "status": "completed",
                                         "label": f"Job {i}"})
    r.user = False
    r.play()
    check("7. backlog: only the spoken jobs are heard, in order",
          ids(r) == ["t2", "t3"])


def t8_interrupt():
    r = Rig(mode="interrupt", in_flight=1)
    r.answer(S, done=False)
    r.play(1)
    r.news(["It built clean."])
    r.floor.fg_done(r.floor._fg_token)
    heard = r.play()
    check("8. (the news really was told mid-answer)",
          heard.index("It built clean.") < heard.index(S[3]))
    check("8. interrupt mode: news told to the end is heard",
          ids(r) == ["a0038b2113e7f9544"])


def t9_parked():
    r = Rig(mode="interrupt", in_flight=1)
    r.answer(S, done=True)
    r.play(1)
    tid = r.news(["Starting the deploy."], close=False)
    r.play(3)
    r.clock.t += 1.5
    r.play()                             # parked; the answer finishes
    check("9. parked news is not heard while its turn is still open",
          ids(r) == [])
    r.floor.handle_event(BgEvent("sentence", tid, text="Deploy finished."))
    r.close(tid)
    r.play()
    check("9. once the rest is told and the turn closed: heard",
          ids(r) == ["a0038b2113e7f9544"])


def t10_no_info():
    r = Rig()
    r.news(["Something happened."], info=None)
    r.play()
    check("10. no task info: nothing recorded", ids(r) == [])
    r = Rig()
    r.news(["Something happened."], info={"description": "x",
                                          "status": "completed"})
    r.play()
    check("10. task info with no id: nothing recorded", ids(r) == [])


def t11_raising_callback():
    def boom(task_id, label=None):
        raise RuntimeError("disk full")
    r = Rig(heard=boom)
    r.news(["It built clean."])
    try:
        heard = r.play()
        ok = heard[-1] == "It built clean."
    except Exception:
        ok = False
    check("11. a heard callback that raises never breaks the floor", ok)


def main():
    for t in (t1_spoken_to_the_end, t2_timing_and_once, t3_key_press,
              t4_silence_playback, t5_capped, t6_never_spoken, t7_backlog,
              t8_interrupt, t9_parked, t10_no_info, t11_raising_callback):
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
