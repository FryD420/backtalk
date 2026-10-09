# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test doubles for the pipe reader and the floor. No CLI, no audio.

FakeClient stands in for ClaudeSDKClient: the test pushes frames (real
SDK message dataclasses, so the reader sees exactly the types it sees
live) onto a queue over time, and receive_messages() hands them out.
It RAISES if two iterators are ever open at once, which is the bug class
the single reader exists to rule out. With replay on (the option the
brain passes), `consume(prompt)` pushes the echo the CLI sends when it
takes a prompt in. A fake `_query._send_control_request` can be made to
sleep, for the rate-limit pull.

FakeMouth stands in for Mouth: it records each chunk as it is enqueued,
plays nothing, and is advanced by hand (`finish()` ends the chunk that
is playing and starts the next). It implements outstanding,
yield_floor and shut_up the way the real one does.
"""
import asyncio
import itertools
import re

from claude_agent_sdk import (AssistantMessage, ResultMessage, StreamEvent,
                              TaskNotificationMessage, TaskStartedMessage,
                              TextBlock, ToolUseBlock, UserMessage)

_END = object()
EVENTS: list = []          # global order of iterator open/close + connects
VIOLATIONS: list = []      # every time a second iterator was attempted
_uid = itertools.count(1)


class FakeQuery:
    def __init__(self):
        self.delay = 0.0
        self.calls = 0

    async def _send_control_request(self, req):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return {"rate_limits": {}}


class FakeClient:
    instances: list = []
    force_no_replay = False        # simulate an old CLI that never echoes
    on_query = None                # hook(client, prompt)
    on_interrupt = None            # hook(client)

    def __init__(self, options=None):
        self.options = options
        extra = getattr(options, "extra_args", None) or {}
        self.replay = ("replay-user-messages" in extra
                       and not FakeClient.force_no_replay)
        self.frames: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.sent_at: list = []
        self.open_iters = 0
        self.max_iters = 0
        self.iterated = 0          # frames handed out
        self.interrupts = 0
        self.connected = False
        self._query = FakeQuery()
        self.n = len(FakeClient.instances)
        FakeClient.instances.append(self)

    # -- lifecycle --
    async def connect(self):
        self.connected = True
        EVENTS.append(("connect", self.n))

    async def disconnect(self):
        self.connected = False
        EVENTS.append(("disconnect", self.n))
        self.frames.put_nowait(_END)

    async def query(self, prompt):
        self.sent.append(prompt)
        self.sent_at.append(asyncio.get_running_loop().time())
        if FakeClient.on_query:
            FakeClient.on_query(self, prompt)

    async def interrupt(self):
        self.interrupts += 1
        if FakeClient.on_interrupt:
            FakeClient.on_interrupt(self)

    async def set_permission_mode(self, mode):
        pass

    # -- the pipe --
    async def receive_messages(self):
        self.open_iters += 1
        self.max_iters = max(self.max_iters, self.open_iters)
        if self.open_iters > 1:
            self.open_iters -= 1
            VIOLATIONS.append(self.n)
            raise RuntimeError("TWO ITERATORS OPEN AT ONCE on one pipe")
        EVENTS.append(("iter_open", self.n))
        try:
            while True:
                m = await self.frames.get()
                if m is _END:
                    return
                if isinstance(m, BaseException):
                    raise m
                self.iterated += 1
                yield m
        finally:
            self.open_iters -= 1
            EVENTS.append(("iter_close", self.n))

    def receive_response(self):
        raise AssertionError("receive_response must never be used")

    # -- test helpers --
    def push(self, *frames):
        for f in frames:
            if isinstance(f, (list, tuple)):
                self.push(*f)
            else:
                self.frames.put_nowait(f)

    def consume(self, prompt):
        """The CLI took `prompt` in: echo it (only when replay is on)."""
        if self.replay:
            self.push(echo(prompt))

    def die(self, exc=None):
        self.push(exc or RuntimeError("CLI crashed"))


# ---- frame builders --------------------------------------------------------

def _se(event, parent=None):
    return StreamEvent(uuid=f"u{next(_uid)}", session_id="s",
                       event=event, parent_tool_use_id=parent)


def text(t, parent=None):
    """A streamed text block: start, one delta per word, stop."""
    out = [_se({"type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""}}, parent)]
    for piece in re.findall(r"\S+\s*", t):
        out.append(_se({"type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": piece}},
                       parent))
    out.append(_se({"type": "content_block_stop", "index": 0}, parent))
    return out


def message_start(parent=None):
    return _se({"type": "message_start", "message": {}}, parent)


def assistant(t=None, tool=None, parent=None):
    content = []
    if t:
        content.append(TextBlock(text=t))
    if tool:
        content.append(ToolUseBlock(id=f"toolu_{next(_uid)}", name=tool[0],
                                    input=tool[1]))
    return AssistantMessage(content=content, model="m",
                            parent_tool_use_id=parent)


def echo(prompt):
    return UserMessage(content=prompt, uuid=f"u{next(_uid)}")


def notif_user(task_id="t1"):
    return UserMessage(
        content=f"<task-notification><task-id>{task_id}</task-id>"
                f"</task-notification>",
        origin={"kind": "task-notification", "producer": "session-task"})


def result(origin=None, cost=0.0):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=False, num_turns=1, session_id="sess",
                         total_cost_usd=cost, usage={"output_tokens": 3},
                         origin=origin)


def task_started(task_id, description, task_type="local_agent"):
    return TaskStartedMessage(subtype="task_started", data={},
                              task_id=task_id, description=description,
                              uuid=f"u{next(_uid)}", session_id="s",
                              task_type=task_type)


def task_done(task_id, status="completed"):
    return TaskNotificationMessage(subtype="task_notification", data={},
                                   task_id=task_id, status=status,
                                   output_file="", summary="",
                                   uuid=f"u{next(_uid)}", session_id="s")


NOTIF = {"kind": "task-notification", "producer": "session-task"}


# ---- the fake mouth --------------------------------------------------------

class Rec:
    __slots__ = ("text", "directions", "started", "finished", "dropped")

    def __init__(self, text, directions=None):
        self.text = text
        self.directions = directions
        self.started = False
        self.finished = False
        self.dropped = False

    def __repr__(self):
        st = ("playing" if self.started and not self.finished else
              "done" if self.finished else
              "dropped" if self.dropped else "queued")
        return f"<{self.text!r} {st}>"


class FakeMouth:
    def __init__(self):
        self.queue: list[Rec] = []     # unfinished, in order; [0] plays
        self.enqueued: list[str] = []  # every chunk, as enqueued
        self.played: list[str] = []    # every chunk that finished playing
        self.cut: list[str] = []       # chunks cut mid-play by shut_up
        self.shut_ups = 0
        self.yields = 0

    @property
    def outstanding(self):
        return len(self.queue)

    @property
    def speaking(self):
        return bool(self.queue)

    def _start(self):
        if self.queue and not self.queue[0].started:
            self.queue[0].started = True

    def say_chunk(self, text, directions=None):
        text = (text or "").strip()
        if not text:
            return None
        r = Rec(text, directions)
        self.queue.append(r)
        self.enqueued.append(text)
        self._start()
        return r

    def say(self, text):
        for s in re.split(r"(?<=[.!?])\s+", text.strip()):
            if s.strip():
                self.say_chunk(s)

    def finish(self):
        """The playing chunk ends; the next one starts."""
        if not self.queue:
            return None
        r = self.queue.pop(0)
        r.finished = True
        self.played.append(r.text)
        self._start()
        return r.text

    def yield_floor(self):
        self.yields += 1
        out, keep = [], []
        for r in self.queue:
            if r.started:
                keep.append(r)
            else:
                r.dropped = True
                out.append((r.text, r.directions))
        self.queue = keep
        return out

    def shut_up(self):
        self.shut_ups += 1
        for r in self.queue:
            if r.started:
                self.cut.append(r.text)
            r.dropped = True
        self.queue = []

    def heard(self):
        """What a listener heard, in order: finished plus cut chunks."""
        return self.played + self.cut


class Unspoken:
    """Collects signals.unspoken calls."""

    def __init__(self):
        self.records = []

    def __call__(self, origin, reason, text, what=None, partial=False):
        self.records.append({"origin": origin, "reason": reason,
                             "text": text, "what": what,
                             "partial": partial})

    def reasons(self):
        return [r["reason"] for r in self.records]


def check_factory(failures):
    def check(name, ok):
        print(("  ok   " if ok else "  FAIL ") + name)
        if not ok:
            failures.append(name)
    return check
