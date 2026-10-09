# backtalk: talk to your Claude Code agent out loud.
# Copyright (C) 2026 Jared Rhodenizer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The floor — one owner of the speaker during turns.

The agent can talk without being asked: a background agent or job
finishes, wakes the model, and it answers. The pipe reader (brain.py)
hands those background (bg) turns over as events; the answer to the
person's own question (fg) arrives through speak_reply. The floor
decides what reaches the mouth, and when:

  IDLE          nothing turn-related playing
  FG_WAIT       a question is out, no answer text yet
  FG_SPEAK      the answer is playing
  INTERRUPTING  the opening line, the news, then the closing line
  BG_SPEAK      news while nothing else is playing
  (USER)        an overlay, not a state: the person has the floor (key
                held, open mic mid-utterance, a permission question
                waiting). Nothing is fed; the state underneath is kept.

It is pure logic. The mouth, the brain, a clock and "does the person
have the floor?" are passed in, so all of it runs in a test with no
audio. The mouth is fed just in time, at most two chunks outstanding
(one playing, one rendered ahead), which is what makes room to slip
news in at a chunk boundary: Mouth.yield_floor() pulls back what has
not started, the news goes in, the answer resumes where it stopped.

background_speech (config): "off" writes bg text to .voice_unspoken
and never speaks it; "idle" speaks it only when nothing else is
playing; "interrupt" also cuts into an answer at a sentence boundary.

Startup lines and voice-console lines do not come through here: they
call mouth.say directly, because they are answers the person asked for.
"""
import asyncio
import re
import time
from collections import deque

from backtalk.config import CFG
from backtalk.jobs import job_label, spoken_name
from backtalk.vlog import log

# <<anything>> is a stage direction: lifted out, never spoken, published on
# the bus when the audio carrying it starts. Bounded so a runaway model cannot
# swallow a paragraph into one "tag".
_DIRECTION_TAG = re.compile(r"<<([^<>]{1,80})>>")
_SENTENCES = re.compile(r"(?<=[.!?])\s+")


def _defence(raw: str, in_fence: bool) -> tuple[str, bool]:
    """Split out fenced-code content (Joe, 2026-08-24): everything between
    ``` markers is for the DASHBOARD, not the mouth — pasted paths, markup,
    whole listing bodies. Returns (speakable text, new fence state). State
    persists across chunks because a fence usually opens in one sentence
    chunk and closes many chunks later."""
    parts = raw.split("```")
    spoken, state = [], in_fence
    for i, part in enumerate(parts):
        if not state:
            spoken.append(part)
        if i < len(parts) - 1:
            state = not state
    return " ".join(spoken), state


class Chunker:
    """Sentences in, speakable chunks out. Shared by answers and news, so
    news never reads out a code block either.

    - fenced code is dashboard-only, never spoken (_defence; OURS, kept:
      upstream has no equivalent)
    - <<directions>> are lifted out and travel with the next chunk, to be
      published when its audio starts (THEIRS, taken over ours: lifting
      the whole tag beats stripping the brackets and reading the body)
    - backticks are never speakable
    - the first sentence ships alone (fast start); the rest go in
      2-sentence breaths, because fuller chunks get livelier prosody.
      single() true means one sentence per chunk (a background agent is
      in flight, so an interrupt can land at the end of the sentence
      being spoken)."""

    def __init__(self, emit, single=None, on_sentence=None):
        self._emit = emit
        self._single = single or (lambda: False)
        self._on_sentence = on_sentence
        self.first = True
        self.in_fence = False
        self.batch: list[str] = []
        self.pending: list[str] = []     # directions waiting for a chunk

    @property
    def empty(self) -> bool:
        """Nothing has been emitted yet."""
        return self.first

    def clean(self, raw: str) -> tuple[str, list]:
        raw, self.in_fence = _defence(raw, self.in_fence)
        dirs = [d.strip() for d in _DIRECTION_TAG.findall(raw) if d.strip()]
        raw = _DIRECTION_TAG.sub(" ", raw)
        return " ".join(raw.replace("`", "").split()).strip(), dirs

    def add(self, s: str, dirs=()):
        if dirs:
            self.pending += list(dirs)
        if not s:
            return
        if self._on_sentence:
            self._on_sentence(s, self.first, list(self.pending))
        if self.first:
            self._emit(s, self.pending or None)
            self.pending = []
            self.first = False
            return
        self.batch.append(s)
        if len(self.batch) >= 2 or self._single():
            self._emit(" ".join(self.batch), self.pending or None)
            self.pending = []
            self.batch = []

    def feed(self, raw: str):
        self.add(*self.clean(raw))

    def flush(self):
        if self.batch:
            self._emit(" ".join(self.batch), self.pending or None)
            self.pending = []
            self.batch = []


# ---- filling in the spoken lines -------------------------------------

DEFAULT_LINES = {
    "interrupt": "Hold on, we're being interrupted. {what} just {status}.",
    "idle": "Heads up. {what} just {status}.",
    "another": "And another one.",
    "resume": "Right, back to what I was saying.",
    "while_you_talked": "Also, while you were talking: {what} {status}.",
    "rest_on_screen": "The rest is on the screen.",
}
_STATUS = {"completed": "finished", "failed": "failed",
           "stopped": "got stopped", "killed": "got stopped"}


def describe_task(info) -> str:
    """The job as the subject of a spoken line: 'the agent building the
    APK', 'the Beepies spike test run', or 'Something' (backtalk/jobs.py).
    No paths, no backticks, no hyphen chains: it is about to be said."""
    return spoken_name(info)


def status_word(info) -> str:
    return _STATUS.get(str((info or {}).get("status") or ""), "finished")


def fill_line(template: str, info) -> str:
    out = (str(template).replace("{what}", describe_task(info))
           .replace("{status}", status_word(info)))
    # a sentence never starts lower-case: "Heads up. the build..." reads
    # wrong on screen and the TTS performs the full stop either way
    return re.sub(r"(^|[.!?]\s+)([a-z])",
                  lambda m: m.group(1) + m.group(2).upper(), out.strip())


# ---- the state machine -------------------------------------------------

IDLE = "idle"
FG_WAIT = "fg_wait"
FG_SPEAK = "fg_speak"
INTERRUPTING = "interrupting"
BG_SPEAK = "bg_speak"

LOOKAHEAD = 2      # chunks outstanding in the mouth: one playing, one ahead
PARK_AFTER_S = 1.0  # news gone quiet this long mid-interrupt: resume answer


class _Fed:
    """A chunk the floor handed to the mouth, and the mouth's record of it."""
    __slots__ = ("kind", "rec", "text", "dirs", "bg")

    def __init__(self, kind, rec, text, dirs, bg):
        self.kind = kind            # fg | ext | bg | line | prompt
        self.rec = rec
        self.text = text
        self.dirs = dirs
        self.bg = bg


class _Bg:
    """One background turn as the floor sees it."""

    def __init__(self, turn_id, info):
        self.id = turn_id
        self.info = info
        self.chunks: deque = deque()     # (kind, text, dirs) ready to feed
        self.chunker = Chunker(
            lambda text, dirs: self.chunks.append(("bg", text, dirs)))
        self.count = 0                   # sentences accepted for speech
        self.overflow: list[str] = []    # over the cap
        self.raw: list[str] = []         # every sentence, as it came
        self.closed = False
        self.ready = False               # has its first chunk
        self.arrived = 0.0
        self.during_user = False
        self.fg_seq = 0
        self.dropped: str | None = None  # given up on: the reason
        self.rest: list[str] = []        # text after it was given up on
        self.last_text = 0.0             # when it last said something
        self.parked = False              # opened, then set aside mid-answer

    @property
    def what(self):
        """The job's short label ('Build the APK'): the log line and the
        .voice_unspoken "what" field. Spoken lines use describe_task."""
        return job_label(self.info)


class Floor:
    def __init__(self, mouth, brain=None, user_active=None, perm_pending=None,
                 clock=time.monotonic, cfg=None, unspoken=None, logf=None):
        cfg = CFG if cfg is None else cfg
        self._log = logf or log
        mode = str(cfg.get("background_speech") or "off").strip().lower()
        if mode not in ("off", "idle", "interrupt"):
            self._log(f"[floor] unknown background_speech {mode!r}, "
                      f"using 'off'")
            mode = "off"
        self.mode = mode
        self.lines = dict(DEFAULT_LINES)
        self.lines.update(cfg.get("background_lines") or {})
        self.max_sentences = int(cfg.get("background_max_sentences", 4))
        self.backlog = int(cfg.get("background_backlog", 3))
        self.hold_max = float(cfg.get("background_hold_max_s", 600))
        self.mouth = mouth
        self.brain = brain
        self._user_active = user_active or (lambda: False)
        self._perm_pending = perm_pending or (lambda: False)
        self._clock = clock
        if unspoken is None:
            from backtalk import signals
            unspoken = signals.unspoken
        self._unspoken = unspoken
        self.state = IDLE
        self._fg_q: deque = deque()      # (kind, text, dirs) not yet fed
        self._fg_active = False
        self._fg_token = 0
        self._fg_seq = 0
        self._need_resume = False
        self._prompts: deque = deque()   # (text, None)
        self._prompt_yield = False
        self._bgs: dict = {}             # open bg turns, by turn id
        self._queue: deque = deque()     # bg turns with news, not started
        self._cur: _Bg | None = None     # the news being told
        self._fed: list[_Fed] = []
        self._stopped = False

    # ---- small helpers -------------------------------------------------

    def _set(self, state):
        if state != self.state:
            self._log(f"[floor] {self.state} -> {state}")
            self.state = state

    def _line(self, key, info):
        return fill_line(self.lines.get(key) or DEFAULT_LINES[key], info)

    def _record(self, origin, reason, text, what=None, partial=False):
        text = " ".join(str(text or "").split())
        if not text and reason != "no_text":
            return
        try:
            self._unspoken(origin, reason, text, what=what, partial=partial)
        except Exception:
            pass

    def _live(self):
        return [f for f in self._fed
                if f.rec is not None and not f.rec.finished
                and not f.rec.dropped]

    def active(self) -> bool:
        """Is anything of the floor's playing or waiting to?"""
        return bool(self._live() or self._fg_q or self._cur or self._queue
                    or self._prompts)

    def single_sentence(self) -> bool:
        """One sentence per chunk while a background agent is in flight,
        so an interrupt lands at the end of the sentence being spoken."""
        try:
            return (self.mode == "interrupt" and self.brain is not None
                    and self.brain.tasks_in_flight > 0)
        except Exception:
            return False

    # ---- the answer (speak_reply) --------------------------------------

    def fg_begin(self) -> int:
        """A question went out. Returns the token its chunks carry."""
        self._fg_token += 1
        self._fg_seq += 1
        self._fg_active = True
        self._need_resume = False
        if self._fg_q:
            self._record("fg", "interrupted",
                         " ".join(t for _, t, _ in self._fg_q))
            self._fg_q.clear()
        if self.state in (IDLE, FG_SPEAK):
            self._set(FG_WAIT)
        return self._fg_token

    def fg_chunk(self, text, directions=None, token=None):
        if token is not None and token != self._fg_token:
            return                      # a reply that was silenced
        self._fg_q.append(("fg", text, directions))
        if self.state in (IDLE, FG_WAIT):
            self._set(FG_SPEAK)
        self.pump()

    def fg_done(self, token=None):
        if token is not None and token != self._fg_token:
            return
        self._fg_active = False
        if self.state == FG_WAIT and not self._fg_q:
            self._set(IDLE)
        self.pump()

    # ---- the permission gate -------------------------------------------

    def prompt(self, text, from_bg=None):
        """A permission question (or its follow-up) takes the floor next:
        it waits while the person holds the key, pulls back whatever has
        not started playing, and is spoken before anything else."""
        if from_bg is None:
            from_bg = (self.brain is not None
                       and getattr(self.brain, "open_origin", None) == "bg")
        if from_bg:
            text = "A background job needs a yes or no. " + text
        for s in [p.strip() for p in _SENTENCES.split(text.strip())]:
            if s:
                self._prompts.append((s, None))
        self._prompt_yield = True
        self.pump()

    # ---- the person takes the floor ------------------------------------

    def _bg_reason(self, reason):
        return "silenced" if reason == "interrupted" else reason

    def silence(self, reason="interrupted"):
        """The person interrupted (key press, typed line, inbox line), or
        we are hanging up. Everything stops at once; nothing vanishes:
        what was cut goes to .voice_unspoken. A background CLI turn is
        NOT interrupted (its work carries on) — the rest of its words go
        to the file with reason 'silenced'."""
        live = self._live()
        self.mouth.shut_up()
        self._fed = []
        fg_rest, bg_rest = [], {}
        for f in live:
            partial = bool(f.rec.started)
            if f.kind in ("fg", "ext"):
                if partial:
                    self._record("fg", reason, f.text, partial=True)
                else:
                    fg_rest.append(f.text)
            elif f.kind == "bg":
                if partial:
                    self._record("bg", self._bg_reason(reason), f.text,
                                 what=f.bg.what if f.bg else None,
                                 partial=True)
                elif f.bg is not None:
                    bg_rest.setdefault(id(f.bg), (f.bg, []))[1].append(f.text)
        fg_rest += [t for _, t, _ in self._fg_q]
        self._fg_q.clear()
        if fg_rest:
            self._record("fg", reason, " ".join(fg_rest))
        self._fg_active = False
        self._fg_token += 1
        self._need_resume = False
        self._prompts.clear()
        self._prompt_yield = False
        cur, self._cur = self._cur, None
        if cur is not None:
            extra = bg_rest.pop(id(cur), (cur, []))[1]
            self._drop_bg(cur, self._bg_reason(reason), extra)
        for b, texts in bg_rest.values():
            self._drop_bg(b, self._bg_reason(reason), texts)
        if reason == "shutdown":
            for b in list(self._queue):
                self._drop_bg(b, "shutdown")
            for b in list(self._bgs.values()):
                if not b.dropped:
                    b.chunker.flush()
                    self._drop_bg(b, "shutdown",
                                  b.raw if self.mode == "off" else ())
        self._set(IDLE)

    def silence_playback(self):
        """A key press while a permission question waits: only playback
        stops (the press is the answer being recorded). The question is
        dropped, everything else stays queued."""
        live = self._live()
        self.mouth.shut_up()
        self._fed = []
        back = []
        for f in live:
            if f.kind in ("fg", "ext"):
                if f.rec.started:
                    self._record("fg", "interrupted", f.text, partial=True)
                else:
                    back.append((f.kind, f.text, f.dirs))
            elif f.kind == "bg" and f.bg is not None:
                if f.rec.started:
                    self._record("bg", "silenced", f.text, what=f.bg.what,
                                 partial=True)
                else:
                    f.bg.chunks.appendleft(("bg", f.text, f.dirs))
        self._fg_q.extendleft(reversed(back))
        self._prompts.clear()
        self._prompt_yield = False

    def shutdown(self):
        """Quit: whatever is held or queued goes to the file ('shutdown')."""
        if not self._stopped:
            self.silence("shutdown")
        self._stopped = True

    # ---- background events (from brain.events) -------------------------

    def handle_event(self, ev):
        k = ev.kind
        if k == "turn_open":
            self._bgs[ev.turn_id] = _Bg(ev.turn_id, ev.info)
        elif k == "sentence":
            b = self._bgs.get(ev.turn_id)
            if b is None:
                b = self._bgs[ev.turn_id] = _Bg(ev.turn_id, ev.info)
            self._bg_sentence(b, ev.text or "")
        elif k == "turn_close":
            b = self._bgs.pop(ev.turn_id, None)
            if b is not None:
                self._bg_close(b, ev.reason)
        elif k == "reset":
            self._void(ev.reason or "rebuild")
        self.pump()

    def _bg_sentence(self, b: _Bg, text: str):
        b.raw.append(text)
        b.last_text = self._clock()
        if self.mode == "off":
            return
        if b.dropped:
            b.rest.append(text)
            return
        s, dirs = b.chunker.clean(text)
        if not s:
            b.chunker.add(s, dirs)
            return
        if b.count >= self.max_sentences:
            if not b.overflow:
                b.chunker.flush()
                b.chunks.append(("line", self._line("rest_on_screen",
                                                    b.info), None))
            b.overflow.append(s)
            return
        b.count += 1
        b.chunker.add(s, dirs)
        if not b.ready and b.chunks:
            self._bg_ready(b)

    def _bg_ready(self, b: _Bg):
        b.ready = True
        b.arrived = self._clock()
        b.during_user = bool(self._user_active())
        b.fg_seq = self._fg_seq
        self._queue.append(b)
        self._log(f"[floor] news from {b.what}"
                  + (" (held: you have the floor)" if b.during_user else ""))
        while len(self._queue) > max(1, self.backlog):
            self._drop_bg(self._queue.popleft(), "backlog")

    def _bg_close(self, b: _Bg, reason):
        b.closed = True
        if self.mode == "off":
            text = " ".join(b.raw).strip()
            if text:
                self._record("bg", "speech_off", text, what=b.what)
            else:
                self._record("bg", "no_text", "", what=b.what)
            return
        b.chunker.flush()
        if not b.ready and b.chunks and not b.dropped:
            self._bg_ready(b)
        if not b.raw:
            # no opening line for a turn that never said anything; the
            # dashboard still learns that it finished
            self._record("bg", "no_text", "", what=b.what)
            return
        if reason in ("rebuild", "brain_lost") and not b.dropped:
            self._drop_bg(b, reason)
        if b.dropped and b.rest:
            self._record("bg", b.dropped, " ".join(b.rest), what=b.what)
            b.rest = []
        if b.overflow:
            self._record("bg", "capped", " ".join(b.overflow), what=b.what)

    def _drop_bg(self, b: _Bg, reason, extra=()):
        """Give up on speaking (the rest of) a background turn: what has
        not been fed goes to the file now, anything it says later goes
        there when it closes."""
        texts = list(extra) + [t for k, t, _ in b.chunks if k == "bg"]
        b.chunks.clear()
        if b.chunker.batch:
            texts += b.chunker.batch
            b.chunker.batch = []
        b.dropped = b.dropped or reason
        try:
            self._queue.remove(b)
        except ValueError:
            pass
        if self._cur is b:
            self._cur = None
        if texts:
            self._record("bg", reason, " ".join(texts), what=b.what)

    def _void(self, reason):
        """The conversation was rebuilt or lost: held news is void."""
        for b in list(self._queue):
            self._drop_bg(b, reason)
        if self._cur is not None:
            self._drop_bg(self._cur, reason)
        for b in list(self._bgs.values()):
            if not b.dropped:
                self._drop_bg(b, reason)

    def _expire(self):
        now = self._clock()
        for b in list(self._queue):
            if now - b.arrived > self.hold_max:
                self._drop_bg(b, "too_old")

    # ---- feeding the mouth ---------------------------------------------

    def pump(self):
        try:
            self._pump()
        except Exception as e:           # the floor must never kill a turn
            self._log(f"[floor] pump error: {e!r}"[:300])

    def _pump(self):
        if self._stopped:
            return                       # hung up: nothing more is said
        self._fed = self._live()
        self._expire()
        if self._user_active():
            return                       # the person has the floor
        # Taking the floor does not wait for room in the mouth: pulling
        # back the unstarted chunks IS what makes the room.
        self._preempt()
        while self.mouth.outstanding < LOOKAHEAD:
            item = self._next()
            if item is None:
                break
            self._feed(*item)

    def _fg_left(self):
        return bool(self._fg_q or self._fg_active or any(
            f.kind in ("fg", "ext") and not f.rec.started for f in self._fed))

    def _preempt(self):
        if self._prompts:
            if self._prompt_yield:
                self._prompt_yield = False
                self._yield()
            return
        if self._perm_pending() or self.state != FG_SPEAK \
                or self.mode != "interrupt":
            return
        b = self._first_ready(skip_held=True)
        if b is not None and self._fg_left():
            self._yield()
            self._set(INTERRUPTING)
            self._start_bg(b, "interrupt")

    def _feed(self, kind, text, dirs, bg):
        rec = self.mouth.say_chunk(text, dirs)
        if rec is None:
            return
        self._fed.append(_Fed(kind, rec, text, dirs, bg))
        if kind in ("line", "prompt"):
            self._log(f"[floor] says: {text}")
        elif kind == "bg":
            self._log(f"[floor] news: {text}")

    def _yield(self):
        """Pull back what has not started and put each piece back where
        it came from, in order."""
        returned = self.mouth.yield_floor()
        mine = [f for f in self._fed
                if f.rec is not None and f.rec.dropped and not f.rec.started]
        self._fed = [f for f in self._fed if f not in mine]
        fg_back, prompts_back = [], []
        resume = self.lines.get("resume") or DEFAULT_LINES["resume"]
        for f in mine:
            if f.kind in ("fg", "ext"):
                fg_back.append((f.kind, f.text, f.dirs))
            elif f.kind == "prompt":
                prompts_back.append((f.text, None))
            elif f.kind == "line" and f.bg is None \
                    and f.text == fill_line(resume, None):
                self._need_resume = True
        # bg pieces go back to the front of their own turn, in order
        by_bg: dict = {}
        for f in mine:
            if f.kind in ("bg", "line") and f.bg is not None:
                by_bg.setdefault(id(f.bg), (f.bg, []))[1].append(f)
        for b, fs in by_bg.values():
            for f in reversed(fs):
                b.chunks.appendleft((f.kind, f.text, f.dirs))
        # anything someone else queued straight into the mouth comes back
        # too, ahead of the answer's own rest
        mine_texts = [f.text for f in mine]
        for text, dirs in returned:
            if text in mine_texts:
                mine_texts.remove(text)
            else:
                fg_back.insert(0, ("ext", text, dirs))
        self._fg_q.extendleft(reversed(fg_back))
        self._prompts.extendleft(reversed(prompts_back))

    def _first_ready(self, skip_held=False):
        """The next news to tell. skip_held is the preempting case: news
        held while the person had the floor, and news already announced
        then parked, both wait for the answer to finish instead."""
        for b in list(self._queue):
            if b.parked and not b.chunks:
                if b.closed:             # announced, and nothing more came
                    self._queue.remove(b)
                continue
            if skip_held and (b.during_user or b.parked):
                continue
            return b
        return None

    def _quiet_enough(self):
        return self.mode == "interrupt" or self.mouth.outstanding == 0

    def _start_bg(self, b: _Bg, line_key):
        try:
            self._queue.remove(b)
        except ValueError:
            pass
        self._cur = b
        b.chunks.appendleft(("line", self._line(line_key, b.info), None))

    def _next(self):
        # 1. the permission gate's own lines go first, always
        if self._prompts:
            text, dirs = self._prompts.popleft()
            return ("prompt", text, dirs, None)
        if self._perm_pending():
            return None
        for _ in range(8):               # a few state hops, never a spin
            st = self.state
            # 2. news being told
            if st in (INTERRUPTING, BG_SPEAK):
                cur = self._cur
                if cur is not None and cur.chunks:
                    kind, text, dirs = cur.chunks.popleft()
                    return (kind, text, dirs, cur)
                if cur is not None and not cur.closed and not cur.dropped:
                    # its turn is still going. When the mouth runs dry,
                    # say the sentence the chunker is holding for a pair
                    # rather than sit on it through a tool call...
                    if self.mouth.outstanding == 0 and cur.chunker.batch:
                        cur.chunker.flush()
                        continue
                    # ...and if it has gone quiet mid-interrupt (a long
                    # tool call), finish the answer instead of holding
                    # it hostage: park the news, pick it up afterwards.
                    if (st == INTERRUPTING and self._fg_left()
                            and self.mouth.outstanding == 0
                            and self._clock() - cur.last_text >= PARK_AFTER_S):
                        self._log(f"[floor] {cur.what} went quiet — back "
                                  f"to the answer, the rest after it")
                        cur.parked = True
                        self._cur = None
                        self._queue.appendleft(cur)
                        self._need_resume = True
                        self._set(FG_SPEAK)
                        continue
                    return None
                self._cur = None
                nxt = self._first_ready()
                if nxt is not None:
                    self._start_bg(nxt, "another")
                    continue
                if st == INTERRUPTING:
                    if self._fg_q or self._fg_active:
                        self._need_resume = True
                        self._set(FG_SPEAK)
                    else:
                        self._set(IDLE)
                elif self._fg_q:
                    self._set(FG_SPEAK)
                elif self._fg_active:
                    self._set(FG_WAIT)
                else:
                    self._set(IDLE)
                continue
            # 3. the answer
            if st == FG_SPEAK:
                if self.mode == "interrupt" and self._fg_left() \
                        and self._first_ready(skip_held=True) is not None:
                    self._preempt()
                    continue
                if self._fg_q:
                    if self._need_resume:
                        self._need_resume = False
                        return ("line", self._line("resume", None), None,
                                None)
                    kind, text, dirs = self._fg_q.popleft()
                    return (kind, text, dirs, None)
                if not self._fg_active:
                    self._set(IDLE)
                    continue
                return None
            if st == FG_WAIT:
                if self._fg_q:
                    self._set(FG_SPEAK)
                    continue
                b = self._first_ready(skip_held=True)
                if b is not None and self._quiet_enough():
                    self._set(BG_SPEAK)
                    self._start_bg(b, "idle")
                    continue
                return None
            # IDLE
            if self._fg_q:
                self._set(FG_SPEAK)
                continue
            b = self._first_ready()
            if b is not None and self._quiet_enough():
                key = ("while_you_talked"
                       if b.during_user and self._fg_seq > b.fg_seq
                       else "idle")
                self._set(BG_SPEAK)
                self._start_bg(b, key)
                continue
            return None
        return None

    # ---- the loop -------------------------------------------------------

    async def run(self):
        """Consume the brain's background events and keep the mouth fed.
        Started once the brain is connected; ends at shutdown()."""
        q = self.brain.events
        while not self._stopped:
            try:
                ev = await asyncio.wait_for(q.get(), 0.05)
            except asyncio.TimeoutError:
                ev = None
            while ev is not None:
                try:
                    self.handle_event(ev)
                except Exception as e:
                    self._log(f"[floor] event error: {e!r}"[:300])
                try:
                    ev = q.get_nowait()
                except asyncio.QueueEmpty:
                    ev = None
            self.pump()
