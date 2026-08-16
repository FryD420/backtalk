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
import re

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from backtalk.config import CFG, DISCIPLINE
from backtalk.vlog import log

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


class WarmBrain:
    def __init__(self, model: str | None = None):
        # Full model id ON PURPOSE — never a bare alias. The SDK
        # resolves aliases through its own bundled CLI and can silently
        # land on an older model.
        self.model = model or CFG["model"]
        self._client: ClaudeSDKClient | None = None
        # True while a query's response hasn't been consumed through its
        # ResultMessage — i.e. the shared message pipe may hold leftovers.
        self._dirty = False

    async def start(self):
        opts = ClaudeAgentOptions(
            cwd=CFG["agent_dir"],
            model=self.model,
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": DISCIPLINE},
            include_partial_messages=True,
            # See config.py: a voice session has no good way to show a
            # permission prompt, and a stalled prompt reads as the AI
            # going mute mid-sentence. Configurable; documented loudly.
            permission_mode=CFG["permission_mode"],
            add_dirs=CFG["extra_dirs"],
        )
        self._client = ClaudeSDKClient(options=opts)
        await self._client.connect()

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

    async def ask_stream(self, utterance: str):
        """Yield complete sentences as they stream out of the model."""
        self._dirty = True             # in flight until its ResultMessage
        await self._client.query(utterance)
        buf = ""
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
                    # End of a speech block (e.g. right before a tool
                    # call): flush NOW. Without this, pre-tool filler
                    # ("On it — let me grab that.") sits silent in the
                    # buffer through the whole tool run, then plays
                    # glued to the answer: long dead air, then two
                    # thoughts at once.
                    tail = buf.strip()
                    buf = ""
                    if tail:
                        yield tail
            elif t == "ResultMessage":
                self._dirty = False    # turn fully consumed — pipe aligned
                break
        tail = buf.strip()
        if tail:
            yield tail


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
