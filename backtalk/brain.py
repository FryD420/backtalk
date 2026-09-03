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
"""
import asyncio
import os
import re
import time
import warnings
from datetime import datetime

import anyio

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

try:
    from claude_agent_sdk import CanUseToolShadowedWarning
except ImportError:                       # older SDKs: nothing to silence
    CanUseToolShadowedWarning = None

from backtalk import signals
from backtalk.config import CFG, DISCIPLINE
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


def load_resume_id(path: str | None = None, now: float | None = None,
                   max_age_s: float = RESUME_MAX_AGE_S) -> str | None:
    """The saved session id, but only while it is still worth resuming.

    Returns None for every flavour of "start fresh": no file, an empty
    one, an unreadable one, or one that has gone cold. A launch must
    never die over a bookkeeping file, so nothing here raises."""
    path = path or SESSION_FILE
    try:
        with open(path) as f:
            sid = f.read().strip()
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if not sid:
        return None
    age = (time.time() if now is None else now) - mtime
    if age > max_age_s:
        log(f"[brain] last session is {age / 3600:.1f}h old — starting "
            f"fresh rather than replaying it (limit "
            f"{max_age_s / 3600:.0f}h)")
        return None
    return sid


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
        # True while a query's response hasn't been consumed through its
        # ResultMessage — i.e. the shared message pipe may hold leftovers.
        self._dirty = False

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
            )
        if resume:
            try:
                self._client = ClaudeSDKClient(options=_opts(resume))
                await self._client.connect()
                log(f"[brain] resumed session {resume[:8]}")
                self.resumed = True
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
        would otherwise hang the voice line mid-conversation."""
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

    async def command(self, cmd: str) -> str:
        """Run a console slash command (/clear, /compact, /model,
        /effort) through the normal stream and return whatever text the
        CLI answered with (confirmations, errors). Slash-command replies
        arrive as COMPLETE AssistantMessages, not stream deltas, so
        ask_stream cannot see them. Bounded like reset_turn is: this
        stream is not trusted to always deliver, and an unbounded await
        here would deafen the whole voice loop. On timeout the pipe is
        left marked dirty so the next reset_turn drains or rebuilds."""
        self._dirty = True
        await self._client.query(cmd)
        texts = []

        async def _collect():
            async for msg in self._client.receive_response():
                t = type(msg).__name__
                if t == "AssistantMessage":
                    for b in getattr(msg, "content", []) or []:
                        txt = getattr(b, "text", None)
                        if txt:
                            texts.append(txt)
                elif t == "ResultMessage":
                    self._dirty = False
                    self._tally(msg, count_turn=False)
                    self._remember_session(msg)
                    break

        try:
            await asyncio.wait_for(_collect(), 90)
        except asyncio.TimeoutError:
            log(f"[brain] console command timed out: {cmd!r}")
            return "error: the command timed out"
        return " ".join(texts).strip()

    async def interrupt(self):
        if self._client:
            await self._client.interrupt()

    async def reset_turn(self, timeout: float = 8.0):
        """Re-align the message pipe after an interrupted/failed turn.

        THE OFF-BY-ONE BUG, and why this method exists: the SDK client
        has ONE shared message stream and receive_response() stops at
        the FIRST ResultMessage it sees — there is no pairing between a
        query and its response. A cancelled turn stops consuming
        mid-stream, leaving the dead turn's remaining messages
        (including its ResultMessage) buffered. The next query then
        pairs with those leftovers: the first ask lands on the stale
        ResultMessage and yields nothing, and every ask after that
        answers the PREVIOUS question — for the rest of the session.
        So: interrupt the dead turn, then drain the pipe through its
        stale ResultMessage before the next query goes out. No-op when
        the last turn was consumed clean."""
        if not self._client or not self._dirty:
            return
        try:
            await asyncio.wait_for(self._client.interrupt(), 5)
        except Exception:
            pass  # turn may already be over — the drain below is the point

        async def _drain() -> int:
            n = 0
            async for msg in self._client.receive_response():
                n += 1
                if type(msg).__name__ == "ResultMessage":
                    break
            return n

        try:
            drained = await asyncio.wait_for(_drain(), timeout)
            log(f"[brain] interrupted turn drained ({drained} stale messages)")
            self._dirty = False
        except Exception:
            # Can't re-align — rebuild the session rather than run
            # desynced. Loses this voice session's conversation memory;
            # better than answering every question one turn late for the
            # rest of the day.
            log("[brain] stream desynced beyond repair — rebuilding the "
                "session (conversation memory for this session resets)")
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
            await self.start()
            self._dirty = False

    async def stop(self):
        if self._client:
            await self._client.disconnect()
            self._client = None

    def _drain_idle(self) -> bool:
        """Throw away whatever a BACKGROUND turn left in the pipe.

        THE OTHER OFF-BY-ONE: the agent can take turns nobody asked for
        — background-task notifications (a finished Bash job, a Monitor
        event, a timeout) wake the model while the mic is quiet, and it
        answers. Nothing here is reading the stream at that moment, so
        that answer (text + ResultMessage) sits buffered. The next real
        question then pairs with it: the person hears the reply to the
        notification, and every answer after that is one question late.
        reset_turn can't see it (the turn wasn't ours, _dirty is False).
        So before every query: pull everything already buffered, non-
        blocking, and log what got dropped. Returns True when the last
        drained message shows a background turn still in flight (text
        without its ResultMessage) — the caller then waits for that
        turn to finish before sending, or the same pairing breaks."""
        q = getattr(self._client, "_query", None)
        rx = getattr(q, "_message_receive", None)
        if rx is None:
            return False
        n, open_turn, texts = 0, False, []
        while True:
            try:
                m = rx.receive_nowait()
            except anyio.WouldBlock:
                break
            except Exception:
                break
            t = m.get("type") if isinstance(m, dict) else None
            if t in ("end", "error"):
                # Lifecycle markers — put them back for receive_messages
                # to handle; nothing after them matters.
                try:
                    q._message_send.send_nowait(m)
                except Exception:
                    pass
                break
            n += 1
            if t == "result":
                open_turn = False
            elif t == "assistant":
                open_turn = True
                for b in (m.get("message", {}) or {}).get("content", []) or []:
                    if isinstance(b, dict) and b.get("type") == "text":
                        texts.append((b.get("text") or "").strip())
            elif t in ("user", "stream_event"):
                open_turn = True
        if n:
            log(f"[brain] dropped {n} buffered messages from a background "
                f"turn (not spoken; see below)")
            for x in texts:
                if x:
                    log(f"[brain] (unspoken) {x[:300]}")
        return open_turn

    async def ask_stream(self, utterance: str):
        """Yield complete sentences as they stream out of the model."""
        if self._drain_idle():
            # A background turn is mid-flight: let it finish (bounded)
            # so our question can't pair with its ResultMessage.
            async def _finish():
                async for msg in self._client.receive_response():
                    if type(msg).__name__ == "ResultMessage":
                        break
            try:
                await asyncio.wait_for(_finish(), 30)
                log("[brain] waited out an in-flight background turn")
            except Exception:
                log("[brain] background turn didn't finish in 30s — "
                    "sending anyway")
        self._dirty = True             # in flight until its ResultMessage
        await self._client.query(utterance)
        buf = ""
        # The activity file on the bus is cleared in the finally: it
        # covers the clean finish (ResultMessage), a cancelled turn (the
        # interrupt lands at the await below and unwinds through here),
        # and a failed one, so no stale "Read: foo.py" ever outlives its
        # turn on the face.
        try:
            async for msg in self._client.receive_response():
                t = type(msg).__name__
                if t == "StreamEvent":
                    ev = getattr(msg, "event", {}) or {}
                    if ev.get("type") == "content_block_delta":
                        delta = ev.get("delta", {}) or {}
                        if delta.get("type") == "text_delta":
                            buf += delta.get("text", "")
                            # emit any complete sentences
                            while True:
                                m = _SENTENCE_END.search(buf)
                                if not m:
                                    break
                                sentence, buf = (buf[:m.end()].strip(),
                                                 buf[m.end():])
                                if sentence:
                                    yield sentence
                    elif ev.get("type") == "content_block_stop":
                        # End of a speech block (e.g. right before a
                        # tool call): flush NOW. Without this, pre-tool
                        # filler ("On it — let me grab that.") sits
                        # silent in the buffer through the whole tool
                        # run, then plays glued to the answer: long
                        # dead air, then two thoughts at once.
                        tail = buf.strip()
                        buf = ""
                        if tail:
                            yield tail
                elif t == "AssistantMessage":
                    # The complete message lands right as its tool
                    # calls run: print what the agent is DOING while
                    # the voice is quiet, so a long silence never reads
                    # as a dead line — and put the same line on the bus
                    # so the face can show it.
                    for b in getattr(msg, "content", []) or []:
                        if type(b).__name__ == "ToolUseBlock":
                            line = _tool_line(b.name, b.input,
                                              prefix=False)
                            log(f"[tool] {line}")
                            signals.activity(line)
                elif t == "ResultMessage":
                    self._dirty = False  # turn fully consumed — aligned
                    self._tally(msg)
                    self._remember_session(msg)
                    # theirs, kept: the plan-usage pull that writes
                    # .voice_rate_limits, which every face now displays.
                    await self._pull_rate_limits()
                    break
        finally:
            signals.turn_end()
        tail = buf.strip()
        if tail:
            yield tail


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
