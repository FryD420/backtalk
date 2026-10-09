# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mouth.yield_floor: pull back what has not started, never cut what has.

Why this exists. Background news is slipped into a reply at a chunk
boundary: the floor pulls back the chunks that have not started, queues
the news, and puts the reply's rest back after it. The tempting way to
pull them back is shut_up(), which bumps the barge-in generation -- and
that cuts the PLAYING chunk mid-word, which is exactly what a boundary
interrupt must never do (TROUBLESHOOTING point 6). So yield_floor flags
renders as dropped instead and leaves _gen alone.

Same fakes as test_mouth_lookahead.py: a fake synth (0.3s to first
audio) and a fake output device that sleeps in real time.

Asserts:
  1. yield_floor returns the unstarted chunks, in order, with their
     stage directions
  2. the playing chunk is left alone and finishes in full
  3. _gen never changes
  4. _pending / outstanding stay correct throughout
  5. reply_done never fires in between (a chunk queued right after the
     yield keeps the reply alive), and fires once at the very end
  6. pulled-back chunks are never played, and the ones still waiting
     in the queue are never even synthesized

Run:  .venv/Scripts/python tests/test_mouth_yield.py
"""
import sys
import time
import types
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backtalk  # noqa: E402

reply_done_calls: list = []
_sig = types.SimpleNamespace(static_stop=lambda: None,
                             set_state=lambda s: None,
                             feed_waveform=lambda p: None,
                             reply_done=lambda: reply_done_calls.append(
                                 time.perf_counter()),
                             direction=lambda d: None)
sys.modules["backtalk.signals"] = _sig
backtalk.signals = _sig


class _Ducker:
    def speech_start(self): pass
    def speech_end(self): pass
    def restore_now(self): pass


_duck = types.SimpleNamespace(Ducker=_Ducker)
sys.modules["backtalk.ducking"] = _duck
backtalk.ducking = _duck

import backtalk.mouth as mouth  # noqa: E402

RATE = 24000
writes: list = []          # (t_start, value, seconds)
synthesized: list = []     # texts the synth was asked for


class FakeOut:
    def __init__(self, samplerate, channels, dtype):
        self.rate = samplerate
        self.active = False

    def start(self):
        self.active = True

    def close(self, ignore_errors=False):
        self.active = False

    def write(self, pcm):
        writes.append((time.perf_counter(), int(pcm[0]) if len(pcm) else 0,
                       len(pcm) / self.rate))
        time.sleep(len(pcm) / self.rate)


VALUES = {}


def fake_synth(text, timeout=30.0):
    synthesized.append(text)
    v = VALUES.setdefault(text, len(VALUES) + 1)
    time.sleep(0.3)
    for i in range(4):                    # 1.2s of audio per chunk
        if i:
            time.sleep(0.05)
        yield RATE, np.full(int(RATE * 0.3), v, dtype=np.int16)


mouth.sd = types.SimpleNamespace(OutputStream=FakeOut)
mouth.synth_stream = fake_synth

failures = []


def check(name, ok):
    print(("  ok   " if ok else "  FAIL ") + name)
    if not ok:
        failures.append(name)


def played_texts():
    inv = {v: k for k, v in VALUES.items()}
    out = []
    for _, v, _ in writes:
        if v and (not out or out[-1] != inv[v]):
            out.append(inv[v])
    return out


def main():
    m = mouth.Mouth()
    recs = [m.say_chunk("Chunk one."), m.say_chunk("Chunk two.", ["glow"]),
            m.say_chunk("Chunk three."), m.say_chunk("Chunk four.")]
    check("say_chunk hands back its record", all(r is not None for r in recs))
    check("outstanding counts all four", m.outstanding == 4)
    deadline = time.time() + 5
    while not recs[0].started and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.4)                       # chunk one is audibly playing
    gen = m._gen
    pulled = m.yield_floor()
    check("1. the unstarted chunks come back in order, directions kept",
          pulled == [("Chunk two.", ["glow"]), ("Chunk three.", None),
                     ("Chunk four.", None)])
    check("3. _gen is untouched", m._gen == gen)
    check("4. one chunk outstanding: the one playing", m.outstanding == 1)
    check("2. the playing chunk is still playing",
          recs[0].started and not recs[0].finished and not recs[0].dropped)
    check("the pulled records are flagged dropped",
          all(r.dropped for r in recs[1:]))
    # the floor queues the news straight away
    news = m.say_chunk("News chunk.")
    check("4. outstanding is two after the news is queued",
          m.outstanding == 2)
    m.wait_done(timeout=15)
    order = played_texts()
    check("2. chunk one played in full, then the news, nothing else",
          order == ["Chunk one.", "News chunk."])
    one = [d for t, v, d in writes if v == VALUES["Chunk one."]]
    check("2. chunk one was not cut (all 1.2s of it was written)",
          abs(sum(one) - 1.2) < 0.01)
    check("5. reply_done fired exactly once, at the end",
          len(reply_done_calls) == 1)
    check("6. pulled-back chunks never played",
          not any(t in order for t in ("Chunk two.", "Chunk three.",
                                       "Chunk four.")))
    check("6. the ones still queued were never synthesized",
          "Chunk three." not in synthesized and
          "Chunk four." not in synthesized)
    check("4. nothing pending, not speaking, records settled",
          m._pending == 0 and not m.speaking and news.finished
          and recs[0].finished and m._order == [])
    check("3. _gen still untouched", m._gen == gen)

    # put the pulled chunks back, as the floor does after the news
    writes.clear()
    for text, dirs in pulled:
        m.say_chunk(text, dirs)
    m.wait_done(timeout=15)
    check("requeued chunks play afterwards, in order",
          played_texts() == ["Chunk two.", "Chunk three.", "Chunk four."])
    check("reply_done fired once more for that reply",
          len(reply_done_calls) == 2)

    # yield with nothing unstarted is a harmless no-op
    check("yield_floor on an idle mouth returns nothing",
          m.yield_floor() == [] and m.outstanding == 0)

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
