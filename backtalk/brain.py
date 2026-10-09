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
"""The warm brain — a persistent Claude session via the Agent SDK,
streaming.

One ClaudeSDKClient lives for the whole voice session: no per-turn
process spawn, no per-turn context reload. Partial-message streaming
means sentences are yielded the moment they're complete, so the mouth
starts speaking while the rest of the thought is still forming.

The session's cwd is YOUR agent's folder (agent_dir in backtalk.json) —
whatever CLAUDE.md lives there defines who is speaking. backtalk adds
only the spoken-delivery discipline (config.DISCIPLINE): the medium,
never the character.

ONE READER OWNS THE PIPE (WarmBrain._read_pipe). The session also takes
turns nobody asked for (a background agent or job finishes and wakes
the model), so the message stream is read continuously, every turn is
attributed (fg: an answer to our question, cmd: a slash command, bg:
nobody asked) by the CLI's echo of each prompt, and ask_stream/command
only consume what the reader routes to them. Background turns go to
self.events for the floor (floor.py) to speak or write down.
"""
import asyncio
import itertools
import os
import re
import time
import warnings
from collections import deque
from datetime import datetime

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

try:
    from claude_agent_sdk import CanUseToolShadowedWarning
except ImportError:                       # older SDKs: nothing to silence
    CanUseToolShadowedWarning = None

from backtalk import signals
from backtalk.config import CFG, DISCIPLINE
from backtalk.jobs import UNKNOWN, job_label
from backtalk.vlog import log

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


SESSION_FILE = os.path.join(CFG["signals_dir"], ".backtalk_session")

# How stale a saved conversation may be and still be worth reattaching
# to. Four hours: long enough to cover a relaunch, lunch, or a crash
# mid-afternoon; short enough that "yesterday" never qualifies.
#
# WHY THERE IS A LIMIT AT ALL (2026-09-03). resume_last_session reattached
# to whatever id was last written, however old. Overnight that id had
# grown into a 2,345-entry, 4.2 MB conversation, and the next morning's
# launch tried to replay the whole thing through an expired prompt cache
# — the most expensive request this machine makes. It did not come back
# inside the startup guard, four launches running, and the voice line was
# dead all morning.
#
# The feature earns its keep on the warm case and is kept. What changed
# is that a COLD session is now a fresh start instead of a gamble: the
# vault carries yesterday, not the transcript, so nothing is actually
# lost by letting it go.
RESUME_MAX_AGE_S = 4 * 3600


# Where a REFUSED conversation waits. The gate above is a good default
# and a blunt one: a long working session that ran past the window is
# exactly the one you most want back. So the escape hatch needs the id,
# and it cannot go and read it later — _remember_session overwrites
# SESSION_FILE with the NEW id on the first completed turn, seconds
# after launch. By the time anyone could ask, the answer is gone.
# (Recovering it by hand on 2026-09-03 meant renaming the file BEFORE
# saying a word to the agent.) So it is parked here at the moment of
# refusal, beside the session file and out of the new session's way.
DECLINED_SUFFIX = ".declined"


def declined_path(path: str | None = None) -> str:
    """Where the refused conversation's id is parked."""
    return (path or SESSION_FILE) + DECLINED_SUFFIX


def resume_decision(path: str | None = None, now: float | None = None,
                    max_age_s: float = RESUME_MAX_AGE_S
                    ) -> tuple[str | None, str | None, float | None]:
    """What to reattach to, what was refused, and how old it was.

    Returns (resume_id, declined_id, age_s). Exactly one of the first
    two is ever set: a session is either warm enough to take or too cold
    and handed back for the escape hatch. Both are None when there is
    nothing saved at all, which is a first launch, not a decision.

    Nothing here raises. A launch must never die over a bookkeeping
    file."""
    path = path or SESSION_FILE
    try:
        with open(path) as f:
            sid = f.read().strip()
        mtime = os.path.getmtime(path)
    except OSError:
        return None, None, None
    if not sid:
        return None, None, None
    age = (time.time() if now is None else now) - mtime
    if age > max_age_s:
        log(f"[brain] last session is {age / 3600:.1f}h old — starting "
            f"fresh rather than replaying it (limit "
            f"{max_age_s / 3600:.0f}h)")
        return None, sid, age
    return sid, None, age


def load_resume_id(path: str | None = None, now: float | None = None,
                   max_age_s: float = RESUME_MAX_AGE_S) -> str | None:
    """The saved session id, but only while it is still worth resuming.

    Returns None for every flavour of "start fresh": no file, an empty
    one, an unreadable one, or one that has gone cold."""
    return resume_decision(path, now, max_age_s)[0]


def park_declined(sid: str | None, path: str | None = None) -> None:
    """Hold a refused conversation where the escape hatch can find it.

    Overwrites: you want the last thing you lost, not the first. Silent
    on failure — a launch must not die because a sidecar would not
    write, it just means the hatch has nothing in it."""
    if not sid:
        return
    try:
        with open(declined_path(path), "w") as f:
            f.write(sid)
    except OSError:
        pass


def load_declined(path: str | None = None) -> str | None:
    """The conversation the last launch refused, if it is still there."""
    try:
        with open(declined_path(path)) as f:
            return f.read().strip() or None
    except OSError:
        return None


def clear_declined(path: str | None = None) -> None:
    """Empty the hatch once it has been used."""
    try:
        os.remove(declined_path(path))
    except OSError:
        pass


async def warmup_or_fresh(brain, mouth, warmup, timeout: float = 180) -> bool:
    """Run the startup warmup, and never let it kill the voice line.

    The warmup is a pleasantry — a silent ping on a fresh session, the
    spoken where-were-we recap on a reattached one. It is not the
    product, and it has no business being fatal.

    Until 2026-09-03 it was: a warmup that ran long called SystemExit(1)
    and took the mic, the inbox on 8795 and the GUI's whole data feed
    with it. The instinct behind that was sound — an earlier bug had the
    greeting play and then nothing happen, silently — so this keeps the
    loud and drops the dying. It says what went wrong, drops the
    reattachment that is the usual culprit, comes up on a fresh
    conversation, and lets the person talk.

    A failed CONNECT is a different thing and stays fatal upstream: no
    brain at all means nothing works. Returns True if the warmup
    completed, False if it was abandoned."""
    try:
        await asyncio.wait_for(warmup(), timeout)
        return True
    except (Exception, asyncio.TimeoutError) as e:
        kind = ("timed out" if isinstance(e, asyncio.TimeoutError)
                else f"failed: {e!r}"[:220])
        log(f"[backtalk] startup warmup {kind} — the voice line stays up")

    if not brain.resumed:
        # Nothing to drop. The connection is live, so this is most
        # likely the model being slow or an upstream incident; say so
        # and let the person ask their question anyway.
        mouth.say("Heads up. My brain didn't answer on the way up, so the "
                  "first thing you ask me might be slow or fail. The voice "
                  "and the face are fine. The log has the error.")
        return False

    log("[backtalk] dropping the reattachment and starting fresh")
    try:
        try:
            await asyncio.wait_for(brain.stop(), 15)
        except Exception:
            pass          # a wedged connection must not block the rescue
        brain.resumed = False
        await asyncio.wait_for(brain.start(), 120)
        mouth.say("I couldn't pick up where we left off — that conversation "
                  "was too big to reload. I've started a fresh one. "
                  "Everything from last time is in the vault.")
    except (Exception, asyncio.TimeoutError) as e:
        log(f"[backtalk] fresh restart failed: {e!r}"[:220])
        mouth.say("I couldn't pick up where we left off, and the fresh "
                  "start didn't take either. The voice and the face are "
                  "fine. The log has the error.")
    return False


# Task statuses that mean a background task is over. Both vocabularies:
# task_notification says "stopped", task_updated says the raw "killed".
_TERMINAL = frozenset({"completed", "failed", "stopped", "killed"})


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).lower()


def _prompt_text(content):
    """The text of a user frame IF it looks like a prompt (a string, or
    text blocks only). Tool results and anything else return None."""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content and all(
            type(b).__name__ == "TextBlock" for b in content):
        return " ".join(getattr(b, "text", "") or "" for b in content)
    return None


class _Ticket:
    """One prompt we sent, waiting for the CLI to take it in. FIFO: the
    echo of its own text (replay-user-messages) is what claims a turn."""
    __slots__ = ("prompt", "norm", "cmdword", "origin", "blind", "turn",
                 "ready", "done")

    def __init__(self, prompt: str, origin: str, blind: bool):
        self.prompt = prompt
        self.norm = _norm(prompt)
        # slash commands may come back in the CLI's own wrapper rather
        # than verbatim, so a command matches on its command word too
        self.cmdword = (self.norm.split()[0]
                        if origin == "cmd" and self.norm.startswith("/")
                        else None)
        self.origin = origin            # "fg" | "cmd"
        self.blind = blind              # sent before any echo was seen
        self.turn: "Turn | None" = None
        self.ready = asyncio.Event()    # set once turn is known (or never)
        self.done = False               # the asker stopped listening


class Turn:
    """One CLI turn as the reader attributes it.

    origin: "fg" (an answer to something we asked), "cmd" (a slash
    command or warmup), "bg" (nobody asked). A CLI turn can be split in
    two here: a question folded into a running background turn turns
    the rest of it fg from the echo on, so the bg Turn closes ("folded")
    and a fg one opens at that frame."""
    _ids = itertools.count(1)

    def __init__(self, origin: str, ticket: _Ticket | None = None):
        self.id = next(Turn._ids)
        self.origin = origin
        self.ticket = ticket
        self.sentences: asyncio.Queue = asyncio.Queue()   # fg: str, None
        self.texts: list[str] = []          # complete AssistantMessage text
        self.closed = asyncio.Event()
        self.result = None
        self.reason: str | None = None
        self.saw_news = False               # news folded into this turn
        self.info: dict | None = None       # bg: the task that woke it
        self.buf = ""
        self.count = 0
        self.lost_text: list[str] = []      # fg text nobody was left to hear


class BgEvent:
    """What the reader tells the floor about turns nobody asked for."""
    TURN_OPEN = "turn_open"
    SENTENCE = "sentence"
    TASK_DONE = "task_done"
    TURN_CLOSE = "turn_close"
    RESET = "reset"          # rebuild / brain lost / clear: held news is void
    __slots__ = ("kind", "turn_id", "text", "info", "reason")

    def __init__(self, kind, turn_id=None, text=None, info=None, reason=None):
        self.kind = kind
        self.turn_id = turn_id
        self.text = text
        self.info = info
        self.reason = reason

    def __repr__(self):
        return (f"BgEvent({self.kind}, turn={self.turn_id}, "
                f"text={self.text!r}, reason={self.reason})")


class WarmBrain:
    def __init__(self, model: str | None = None, can_use_tool=None,
                 resume_id: str | None = None):
        # Full model id ON PURPOSE — never a bare alias. The SDK
        # resolves aliases through its own bundled CLI and can silently
        # land on an older model.
        self.model = model or CFG["model"]
        # The spoken permission gate (main.py builds it). Wired at
        # connect in EVERY mode, so a live mode flip needs no reconnect;
        # bypass simply never consults it.
        self._can_use_tool = can_use_tool
        # Session usage, spoken on request ("usage report").
        self.session = {"turns": 0, "out_tokens": 0, "in_tokens": 0,
                        "cost": 0.0}
        self._client: ClaudeSDKClient | None = None
        # The session to reattach to at the FIRST start only (config key
        # resume_last_session). Consumed on use: a desync rebuild in
        # reset_turn() must always start FRESH: a rebuild means a turn
        # went sideways mid-stream, the wrong moment to gamble on
        # reattaching. (Community proposal, issue #1.)
        self._resume_id = resume_id
        # True once start() actually reattached to a saved conversation
        # (main.py speaks a where-were-we recap instead of the silent
        # warmup ping in that case).
        self.resumed = False
        # THE SINGLE PIPE READER (see _read_pipe). It belongs to the
        # client it was started for; stop() cancels it and waits before
        # disconnecting, start() spawns the next one.
        self._reader: asyncio.Task | None = None
        self._open: Turn | None = None          # the turn frames go to
        self._tickets: list[_Ticket] = []       # sent, not yet claimed
        self._tasks: dict = {}                  # task id -> info
        self._done: deque = deque(maxlen=10)    # finished, not yet claimed
        self._side: set = set()                 # spawned side tasks
        # Background news for the floor (floor.py). Unbounded on purpose:
        # the reader must never wait on anyone, or the SDK's 100-message
        # buffer fills and the CLI stalls (spec 2026-10-08, section 1).
        self.events: asyncio.Queue = asyncio.Queue()
        # The reader died (CLI crash, error frame). The next question
        # rebuilds the session, the way reset_turn's rebuild does.
        self.lost = False
        # True once this connection has echoed a prompt back. Until then
        # attribution falls back to turn order (and fg_hold_s).
        self._echo_seen = False

    # ---- what other modules may look at -------------------------------

    @property
    def open_origin(self) -> str | None:
        """Origin of the turn currently streaming ("fg"/"cmd"/"bg"), or
        None between turns. The permission gate uses it to say when a
        background job is the one asking."""
        return self._open.origin if self._open is not None else None

    @property
    def tasks_in_flight(self) -> int:
        """Background tasks started and not yet finished."""
        return sum(1 for i in self._tasks.values()
                   if i.get("status") not in _TERMINAL)

    # ---- lifecycle ----------------------------------------------------

    async def start(self):
        mode = CFG["permission_mode"]
        if mode == "default":
            mode = "ask"     # legacy alias, see config.py
        # backtalk's "ask" = the SDK's "default" mode with gated calls
        # routed to the spoken can_use_tool gate.
        sdk_mode = "default" if mode == "ask" else mode
        if sdk_mode == "bypassPermissions" and self._can_use_tool \
                and CanUseToolShadowedWarning:
            # Deliberate auto-approve: the SDK warns that the callback is
            # shadowed. That IS the chosen behavior, so boot quietly.
            warnings.filterwarnings("ignore",
                                    category=CanUseToolShadowedWarning)
        resume, self._resume_id = self._resume_id, None   # consume once

        def _opts(rid):
            return ClaudeAgentOptions(
                cwd=CFG["agent_dir"],
                model=self.model,
                system_prompt={"type": "preset", "preset": "claude_code",
                               "append": DISCIPLINE},
                include_partial_messages=True,
                permission_mode=sdk_mode,
                can_use_tool=self._can_use_tool,
                add_dirs=CFG["extra_dirs"],
                # SDK default is 1 MB per stream-json message; a 1080p
                # screenshot read is ~5 MB base64 and killed the session.
                max_buffer_size=16 * 1024 * 1024,
                skills=CFG["visible_skills"],
                resume=rid,
                # The CLI echoes every prompt back at the moment it TAKES
                # IT IN, including when it folds one into a running turn
                # (confirmed live, 2026-10-08). That echo is what ties an
                # answer to its question; see _read_pipe.
                extra_args={"replay-user-messages": None},
            )
        if resume:
            try:
                self._client = ClaudeSDKClient(options=_opts(resume))
                await self._client.connect()
                log(f"[brain] resumed session {resume[:8]}")
                self.resumed = True
                self._spawn_reader()
                return
            except Exception as e:
                # a stale or invalid saved session must never brick the
                # launch. Fall back to a fresh conversation and say so.
                log(f"[brain] resume failed ({str(e)[:80]}), "
                    f"starting fresh")
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
        self._client = ClaudeSDKClient(options=_opts(None))
        await self._client.connect()
        self._spawn_reader()

    def _spawn_reader(self):
        self.lost = False
        self._echo_seen = False
        self._reader = asyncio.get_running_loop().create_task(
            self._read_pipe(self._client), name="backtalk-pipe-reader")

    async def reattach(self, sid: str):
        """Swap this live brain onto an older conversation.

        The escape hatch behind the resume gate: the gate refused a cold
        session at launch, and the person wants it anyway. Goes through
        the ordinary start() path on purpose, so the resume-failed
        fallback there still applies — if the old conversation will not
        load, this comes up fresh rather than leaving no brain at all.

        Whatever was said since launch stays behind; the caller says so
        out loud before calling this."""
        await self.stop()
        self._resume_id = sid
        self.resumed = False
        await self.start()

    async def set_permission_mode(self, backtalk_mode: str):
        """Live flip, no reconnect, conversation intact ("ask" maps to
        the SDK's "default", whose gated calls hit the spoken gate)."""
        if self._client:
            sdk_mode = "default" if backtalk_mode == "ask" \
                else backtalk_mode
            await self._client.set_permission_mode(sdk_mode)

    async def context_usage(self):
        """The CLI's own context-window breakdown, or None."""
        try:
            return await self._client.get_context_usage()
        except Exception:
            return None

    def _remember_session(self, rm):
        """Persist the session id after a completed turn, so the next
        launch can reattach (config: resume_last_session). Must never
        break a turn; silence on any failure."""
        if not CFG.get("resume_last_session"):
            return
        sid = getattr(rm, "session_id", None)
        if not sid:
            return
        try:
            with open(SESSION_FILE, "w") as f:
                f.write(sid)
        except OSError:
            pass

    def _tally(self, rm, count_turn=True):
        """Session usage bookkeeping. Must never break a turn."""
        try:
            u = getattr(rm, "usage", None) or {}
            s = self.session
            if count_turn:
                s["turns"] += 1
            s["out_tokens"] += int(u.get("output_tokens") or 0)
            s["in_tokens"] += (int(u.get("input_tokens") or 0)
                               + int(u.get("cache_read_input_tokens")
                                     or 0))
            c = getattr(rm, "total_cost_usd", None)
            if c:
                s["cost"] += float(c)
        except Exception:
            pass

    async def _pull_rate_limits(self):
        """Ask the CLI outright how much of the plan is spent.

        A DIRECT QUERY, not the RateLimitEvent stream. The event fires
        rarely and usually arrives carrying resets_at with no utilization
        at all, so a listener built on it reports nothing most of the
        time -- which is exactly how this feature looked broken for its
        whole life. (Community fix, ai-visualizer issue #1.)

        THIS REACHES PAST THE SDK'S PUBLIC SURFACE ON PURPOSE, and a
        reader should know it rather than discover it. `get_usage` is a
        control request the bundled CLI answers but the SDK never wraps,
        so there is no supported call to make. The supported-looking
        alternative is a dead end and was tested as one: the terminal
        status line never fires in a headless session, so its numbers
        are unreachable from here.

        Which means this can stop working without anyone doing anything
        wrong, and the containment is the point. Every failure is
        swallowed and the readout simply goes quiet. It must never cost
        a turn, so it is also bounded -- an unanswered control request
        would otherwise hang the voice line mid-conversation.

        Spawned as its own task by the pipe reader, NEVER awaited by it:
        five seconds of a paused reader is exactly the backpressure stall
        the reader exists to prevent."""
        if not CFG.get("show_usage"):
            return
        try:
            usage = await asyncio.wait_for(
                self._client._query._send_control_request(
                    {"subtype": "get_usage"}), 5)
            for window in ("five_hour", "seven_day"):
                w = (usage.get("rate_limits") or {}).get(window)
                if not w:
                    continue
                # Two spellings accepted deliberately: this shape is not
                # documented anywhere, so the cheap tolerance is worth
                # more than the tidiness. Both are percentages, and the
                # rest of the pipeline wants a 0..1 fraction.
                pct = w.get("utilization")
                if pct is None:
                    pct = w.get("used_percentage")
                pct = pct / 100 if pct is not None else None
                resets = w.get("resets_at")
                if isinstance(resets, str):
                    resets = int(datetime.fromisoformat(resets).timestamp())
                signals.set_rate_limit(window, pct, resets)
        except Exception:
            pass

    # ---- the consumers: they never touch the pipe ----------------------

    def _new_ticket(self, prompt: str, origin: str) -> _Ticket:
        # abandoned questions whose echo never came: keep a few (a late
        # echo still pairs with its own dead question), not forever
        dead = [t for t in self._tickets if t.done and t.turn is None]
        for t in dead[:-4]:
            self._tickets.remove(t)
        tk = _Ticket(prompt, origin, blind=not self._echo_seen)
        self._tickets.append(tk)
        return tk

    async def _ensure_alive(self):
        """A dead reader means a dead pipe: rebuild before asking."""
        if self.lost or self._client is None or self._reader is None:
            log("[brain] the pipe reader is gone — rebuilding the session "
                "(conversation memory for this session resets)")
            await self._rebuild()

    async def _hold_if_blind(self) -> bool:
        """Only when this connection has never echoed a prompt (an old
        CLI): a question sent into an open turn could be folded into it
        with nothing to mark where the answer starts. So hold it until
        that turn ends, at most fg_hold_s. Returns True when the hold
        ran out with the turn still open: the caller then counts that
        turn as the answer from the moment the question goes out."""
        t = self._open
        if self._echo_seen or t is None:
            return False
        try:
            hold = float(CFG.get("fg_hold_s") or 20)
        except (TypeError, ValueError):
            hold = 20.0
        log(f"[reader] no prompt echo seen yet and a turn is open — "
            f"holding the question up to {hold:g}s")
        try:
            await asyncio.wait_for(t.closed.wait(), hold)
            return False
        except asyncio.TimeoutError:
            return self._open is t

    async def command(self, cmd: str) -> str:
        """Run a console slash command (/clear, /compact, /model,
        /effort) and return whatever text the CLI answered with
        (confirmations, errors). Slash-command replies arrive as
        COMPLETE AssistantMessages, not stream deltas, so the reader
        collects them on the command's own turn. Bounded: an unbounded
        await here would deafen the whole voice loop. On timeout the
        turn, if one opened, is left open for the next reset_turn."""
        await self._ensure_alive()
        tk = self._new_ticket(cmd, "cmd")
        await self._client.query(cmd)

        async def _settled():
            await tk.ready.wait()
            if tk.turn is not None:
                await tk.turn.closed.wait()

        try:
            await asyncio.wait_for(_settled(), 90)
        except asyncio.TimeoutError:
            tk.done = True
            log(f"[brain] console command timed out: {cmd!r}")
            return "error: the command timed out"
        tk.done = True
        if _norm(cmd).startswith("/clear"):
            # a cleared conversation: whatever news was held belongs to
            # the conversation that just went away
            self._forget_tasks("rebuild")
        t = tk.turn
        return " ".join(t.texts).strip() if t is not None else ""

    async def interrupt(self):
        """Stop OUR turn. A background turn is never interrupted: its
        work carries on, and only its speech is silenced (floor.py)."""
        if not self._client:
            return
        if self._open is not None and self._open.origin == "bg":
            log("[brain] not interrupting: the open turn is background "
                "work, only its speech is silenced")
            return
        await self._client.interrupt()

    async def reset_turn(self, timeout: float = 8.0):
        """Make sure no turn of OURS is still running before the next
        query goes out.

        THE OFF-BY-ONE BUG this used to fix by draining: the SDK has ONE
        shared message stream and no pairing between a query and its
        response, so a cancelled turn's leftovers used to pair with the
        next question. The single pipe reader (_read_pipe) makes the
        pairing explicit instead, so there is nothing left to drain.
        What remains is courtesy to the CLI: interrupt an open fg/cmd
        turn and wait for its ResultMessage. If it never comes the
        session is rebuilt rather than left wedged. No-op when no turn
        of ours is open; a background turn is left running."""
        t = self._open
        if not self._client or t is None or t.origin == "bg":
            return
        try:
            await asyncio.wait_for(self._client.interrupt(), 5)
        except Exception:
            pass  # turn may already be over — the wait below is the point
        try:
            await asyncio.wait_for(t.closed.wait(), timeout)
            log(f"[brain] interrupted turn {t.id} closed cleanly")
        except asyncio.TimeoutError:
            # Can't confirm it ended — rebuild rather than run against a
            # wedged turn. Loses this voice session's conversation memory.
            log("[brain] interrupted turn never closed — rebuilding the "
                "session (conversation memory for this session resets)")
            await self._rebuild()

    async def _rebuild(self):
        await self._stop_reader()
        self._close_all("rebuild")
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None
        await self.start()

    async def _stop_reader(self):
        """Cancel the reader and WAIT for it, so two readers can never
        overlap on one pipe (or a dead client's reader on a new one)."""
        task, self._reader = self._reader, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for side in list(self._side):
            side.cancel()

    async def stop(self):
        await self._stop_reader()
        self._close_all("rebuild")
        if self._client:
            await self._client.disconnect()
            self._client = None

    async def ask_stream(self, utterance: str):
        """Yield complete sentences of the answer to THIS question.

        Never reads the pipe: it files a ticket, sends the question and
        reads the sentences the reader routes to the ticket's turn. The
        activity file on the bus is cleared in the finally: it covers
        the clean finish, a cancelled turn and a failed one, so no stale
        "Read: foo.py" ever outlives its turn on the face."""
        await self._ensure_alive()
        forced = await self._hold_if_blind()
        tk = self._new_ticket(utterance, "fg")
        try:
            await self._client.query(utterance)
            if forced and self._open is not None and self._open.ticket is None:
                log("[reader] no echo and the open turn ran past "
                    "fg_hold_s — counting it as the answer from here")
                self._attach(tk)
            await tk.ready.wait()
            t = tk.turn
            if t is None:
                return
            while True:
                s = await t.sentences.get()
                if s is None:
                    break
                yield s
        finally:
            tk.done = True
            signals.turn_end()

    # ---- THE SINGLE PIPE READER ---------------------------------------

    async def _read_pipe(self, client):
        """The only code anywhere that iterates the SDK's message stream.

        WHY ONE READER (2026-10-08). The agent takes turns nobody asked
        for: a background agent or job finishes and wakes the model, and
        it answers. With the pipe read only while one of OUR turns ran,
        that answer sat in the SDK's 100-message buffer, and the SDK's
        own reader does a BLOCKING send into that buffer in the same loop
        that answers permission prompts and hooks. A background turn
        filled it in seconds, froze mid-sentence with its permission
        requests unanswered, and the rest of it was paired with the
        person's next question. Reading every frame the moment it lands
        means the buffer never fills.

        So this drains everything, always, and routes it:
          - a turn opens at its first main-thread frame and closes at
            its ResultMessage. Sub-agent frames (parent_tool_use_id set)
            never open a turn and are never spoken.
          - the echo of a prompt we sent (replay-user-messages) claims
            everything after it, to the next ResultMessage, for that
            prompt's ticket: an fg answer, or a cmd reply. A turn that
            opens with no echo is background (bg).
          - fg sentences go to the ticket's turn queue (ask_stream), bg
            ones to self.events (the floor), cmd text is collected.

        It never awaits anything slow: queues are unbounded, file writes
        are tiny and synchronous, and the rate-limit pull is spawned."""
        log("[reader] listening on the message pipe")
        why = "the message stream ended"
        try:
            async for msg in client.receive_messages():
                try:
                    self._on_frame(msg)
                except Exception as e:
                    log(f"[reader] frame skipped after an error: {e!r}"[:300])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            why = f"{e!r}"[:200]
        if client is self._client:
            self._lose(why)

    def _lose(self, why: str):
        self.lost = True
        log(f"[reader] the pipe reader died ({why}) — the next question "
            f"rebuilds the session")
        self._close_all("brain_lost")

    def _emit(self, ev: BgEvent):
        self.events.put_nowait(ev)

    def _spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._side.add(task)
        task.add_done_callback(self._side.discard)

    def _on_frame(self, msg):
        kind = type(msg).__name__
        if kind == "StreamEvent":
            self._on_stream(msg)
        elif kind == "AssistantMessage":
            self._on_assistant(msg)
        elif kind == "UserMessage":
            self._on_user(msg)
        elif kind == "ResultMessage":
            self._on_result(msg)
        elif isinstance(getattr(msg, "subtype", None), str):
            self._on_system(msg)

    def _on_stream(self, msg):
        if getattr(msg, "parent_tool_use_id", None) is not None:
            return                      # sub-agent: never spoken
        t = self._turn_for_frame()
        ev = getattr(msg, "event", {}) or {}
        et = ev.get("type")
        if et == "content_block_delta":
            delta = ev.get("delta", {}) or {}
            if delta.get("type") == "text_delta":
                t.buf += delta.get("text", "")
                while True:
                    m = _SENTENCE_END.search(t.buf)
                    if not m:
                        break
                    sentence, t.buf = (t.buf[:m.end()].strip(),
                                       t.buf[m.end():])
                    if sentence:
                        self._sentence(t, sentence)
        elif et == "content_block_stop":
            # End of a speech block (e.g. right before a tool call):
            # flush NOW. Without this, pre-tool filler ("On it — let me
            # grab that.") sits silent in the buffer through the whole
            # tool run, then plays glued to the answer.
            tail = t.buf.strip()
            t.buf = ""
            if tail:
                self._sentence(t, tail)

    def _on_assistant(self, msg):
        sub = getattr(msg, "parent_tool_use_id", None) is not None
        t = self._open if sub else self._turn_for_frame()
        for b in getattr(msg, "content", []) or []:
            bn = type(b).__name__
            if bn == "ToolUseBlock":
                # The complete message lands right as its tool calls run:
                # print what the agent is DOING while the voice is quiet,
                # and put the same line on the bus for the face.
                line = _tool_line(b.name, b.input, prefix=False)
                if t is not None and t.origin == "bg":
                    line = "background: " + line
                log(f"[tool] {line}")
                signals.activity(line)
            elif bn == "TextBlock" and not sub and t is not None:
                txt = getattr(b, "text", None)
                if txt:
                    t.texts.append(txt)

    def _on_user(self, msg):
        if getattr(msg, "parent_tool_use_id", None) is not None:
            return
        origin = getattr(msg, "origin", None)
        okind = origin.get("kind") if isinstance(origin, dict) else None
        text = _prompt_text(getattr(msg, "content", None))
        if text is not None and okind in (None, "human"):
            tk = self._match(text)
            if tk is not None:
                self._echo_seen = True
                self._attach(tk)
            else:
                log(f"[reader] a replayed prompt matched no question "
                    f"(ignored): {text[:80]!r}")
            return
        if okind and okind != "human":
            # news injected into the conversation (a task notification,
            # a peer or channel message)
            self._claim_news(self._turn_for_frame())
            return
        self._turn_for_frame()          # a tool result: part of the turn

    def _on_result(self, msg):
        t = self._open
        if t is None:
            tk = self._fallback_ticket()
            if tk is None:
                log("[reader] a result arrived with no turn open (ignored)")
                return
            t = self._attach(tk)
        ro = getattr(msg, "origin", None)
        rkind = ro.get("kind") if isinstance(ro, dict) else None
        if t.origin == "bg" and rkind in (None, "human"):
            # CROSS-CHECK: the CLI says this turn answered a prompt WE
            # sent, but no echo claimed it. Give the waiting asker its
            # end-of-turn rather than leave it hanging forever.
            tk = next((x for x in self._tickets
                       if x.turn is None and not x.done), None)
            if tk is not None:
                log(f"[reader] cross-check: turn {t.id} answered our "
                    f"{tk.origin} prompt but its echo never matched")
                self._tickets.remove(tk)
                tk.turn = t if tk.origin == "cmd" else None
                tk.ready.set()
        elif t.origin != "bg" and rkind not in (None, "human"):
            log(f"[reader] turn {t.id} ({t.origin}) was folded into a "
                f"{rkind} turn")
        self._close(t, "result", msg)
        if t.origin == "fg":
            self._tally(msg)
            self._remember_session(msg)
            signals.turn_end()
            # theirs, kept: the plan-usage pull that writes
            # .voice_rate_limits. Spawned, never awaited (see docstring).
            self._spawn(self._pull_rate_limits())
        elif t.origin == "cmd":
            self._tally(msg, count_turn=False)
            self._remember_session(msg)
        elif not any(x.turn is None and not x.done for x in self._tickets):
            signals.turn_end()          # no stale background activity line

    def _on_system(self, msg):
        sub = getattr(msg, "subtype", None)
        data = getattr(msg, "data", None) or {}
        tid = getattr(msg, "task_id", None) or data.get("task_id")
        if not tid:
            return
        if sub == "task_started":
            self._tasks[tid] = {
                "id": tid,
                "description": (getattr(msg, "description", None)
                                or data.get("description") or ""),
                "type": (getattr(msg, "task_type", None)
                         or data.get("task_type")),
                "status": "running"}
            # one short name for the job, shared by the log, the floor's
            # lines and .voice_unspoken (backtalk/jobs.py)
            self._tasks[tid]["label"] = job_label(self._tasks[tid])
        elif sub == "task_notification":
            self._task_done(tid, getattr(msg, "status", None)
                            or data.get("status"))
        elif sub == "task_updated":
            st = (getattr(msg, "status", None)
                  or (data.get("patch") or {}).get("status"))
            if st in _TERMINAL:
                self._task_done(tid, st)

    def _task_done(self, tid, status):
        info = self._tasks.get(tid)
        if info is None:                # never saw it start: "Something"
            info = {"id": tid, "description": "", "type": None,
                    "label": UNKNOWN}
            self._tasks[tid] = info
        if info.get("status") in _TERMINAL:
            return                      # reported twice: once is enough
        info["status"] = status or "completed"
        self._done.append(info)
        log(f"[reader] task \"{info.get('label') or UNKNOWN}\" "
            f"{info['status']} ({self.tasks_in_flight} still running)")
        self._emit(BgEvent(BgEvent.TASK_DONE, info=dict(info)))

    def _forget_tasks(self, reason: str):
        self._tasks.clear()
        self._done.clear()
        self._emit(BgEvent(BgEvent.RESET, reason=reason))

    # ---- attribution -----------------------------------------------------

    def _match(self, text: str) -> _Ticket | None:
        """FIFO, content equality (after whitespace), the oldest first.
        Live tickets before abandoned ones: a question asked again after
        an interrupt must never lose its echo to the dead copy."""
        n = _norm(text)
        for want_done in (False, True):
            for tk in self._tickets:
                if tk.turn is not None or tk.done != want_done:
                    continue
                if tk.norm == n or (len(tk.norm) >= 8 and tk.norm in n):
                    return tk
                if tk.cmdword and tk.cmdword in n:
                    return tk
        return None

    def _fallback_ticket(self) -> _Ticket | None:
        """A turn (or result) arrived with no echo in front of it. It is
        background, unless the oldest live ticket is a slash command
        (local commands are not guaranteed to echo) or this connection
        has never echoed anything (an old CLI: turn order is all we
        have)."""
        tk = next((x for x in self._tickets
                   if x.turn is None and not x.done), None)
        if tk is None:
            return None
        if tk.origin == "cmd" or (tk.blind and not self._echo_seen):
            return tk
        return None

    def _attach(self, tk: _Ticket) -> Turn:
        """From this frame on, the turn belongs to the ticket. A turn
        that was already open (a background turn the question was
        folded into, or an older question's) ends here."""
        old = self._open
        if old is not None:
            self._close(old, "folded")
        # anything older than this ticket whose asker has gone, and whose
        # echo will evidently never come, is dead weight
        # (except an identical question: _match handed this echo to the
        # live copy, so the dead copy's own echo is still to come)
        idx = self._tickets.index(tk) if tk in self._tickets else -1
        for stale in self._tickets[:max(idx, 0)]:
            if stale.done and stale.turn is None and stale.norm != tk.norm:
                self._tickets.remove(stale)
                stale.ready.set()
        if tk in self._tickets:
            self._tickets.remove(tk)
        t = Turn(tk.origin, tk)
        self._open = t
        tk.turn = t
        tk.ready.set()
        log(f"[reader] turn {t.id} opened ({t.origin}"
            + (f", folded into turn {old.id}" if old is not None else "")
            + ")")
        return t

    def _turn_for_frame(self) -> Turn:
        if self._open is not None:
            return self._open
        tk = self._fallback_ticket()
        if tk is not None:
            log(f"[reader] a turn opened with no echo — pairing it with "
                f"the pending {tk.origin} (fallback)")
            return self._attach(tk)
        t = Turn("bg")
        t.info = self._done.popleft() if self._done else None
        self._open = t
        what = (t.info or {}).get("label") or "unknown source"
        log(f"[reader] turn {t.id} opened (bg: {what})")
        self._emit(BgEvent(BgEvent.TURN_OPEN, t.id, info=t.info))
        return t

    def _claim_news(self, t: Turn):
        info = self._done.popleft() if self._done else None
        what = (info or {}).get("label") or "a notification"
        if t.origin == "bg":
            log(f"[reader] news ({what}) folded into background turn {t.id}")
            return
        t.saw_news = True
        log(f"[reader] news ({what}) folded into turn {t.id} — the model "
            f"tells it, no template")

    def _sentence(self, t: Turn, s: str):
        t.count += 1
        if t.origin == "fg":
            if t.ticket is not None and t.ticket.done:
                t.lost_text.append(s)
            else:
                t.sentences.put_nowait(s)
        elif t.origin == "bg":
            self._emit(BgEvent(BgEvent.SENTENCE, t.id, text=s))

    def _close(self, t: Turn, reason: str, result=None):
        tail = t.buf.strip()
        t.buf = ""
        if tail:
            self._sentence(t, tail)
        t.reason = reason
        t.result = result
        if t.origin == "fg":
            if t.ticket is not None and t.ticket.done:
                while not t.sentences.empty():
                    s = t.sentences.get_nowait()
                    if s is not None:
                        t.lost_text.append(s)
                if t.lost_text:
                    signals.unspoken(
                        "fg", "interrupted" if reason in ("result", "folded")
                        else reason, " ".join(t.lost_text))
            t.sentences.put_nowait(None)
        elif t.origin == "bg":
            self._emit(BgEvent(BgEvent.TURN_CLOSE, t.id, info=t.info,
                               reason=reason))
        t.closed.set()
        if self._open is t:
            self._open = None
        log(f"[reader] turn {t.id} closed ({t.origin}, {reason}, "
            f"{t.count} sentences"
            + (", news folded in" if t.saw_news else "") + ")")

    def _close_all(self, reason: str):
        if self._open is not None:
            self._close(self._open, reason)
        for tk in self._tickets:
            tk.ready.set()              # turn stays None: nothing coming
        self._tickets.clear()
        self._forget_tasks(reason)


def _tool_line(name: str, inp, prefix: bool = True) -> str:
    """One line per tool call — the file, command, pattern or query that
    says what the agent is up to, not the raw JSON. prefix=True adds the
    terminal's "[tool] " tag; the bare form goes on the signal bus."""
    inp = inp if isinstance(inp, dict) else {}
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        what = inp.get("file_path") or inp.get("notebook_path") or ""
    elif name == "Bash":
        what = inp.get("description") or inp.get("command") or ""
    elif name in ("Grep", "Glob"):
        what = inp.get("pattern") or ""
        if inp.get("path"):
            what = f"{what} in {inp['path']}"
    elif name == "WebFetch":
        what = inp.get("url") or ""
    elif name == "WebSearch":
        what = inp.get("query") or ""
    elif name in ("Agent", "Task"):
        what = inp.get("description") or ""
    elif name == "Skill":
        what = inp.get("skill") or ""
    else:
        what = ", ".join(f"{k}={str(v)[:40]}"
                         for k, v in list(inp.items())[:3])
    what = " ".join(str(what).split())
    if len(what) > 100:
        what = what[:97] + "..."
    line = f"{name}: {what}" if what else name
    return f"[tool] {line}" if prefix else line


if __name__ == "__main__":
    import time

    async def demo():
        b = WarmBrain()
        await b.start()
        for prompt in ("Voice check: greet me in one sentence.",
                       "And what's two plus two, spoken like yourself?"):
            t0 = time.time()
            async for s in b.ask_stream(prompt):
                print(f"  ({time.time()-t0:4.1f}s) {s}", flush=True)
        await b.stop()

    asyncio.run(demo())
