"""backtalk — talk to your Claude Code agent out loud.

Flow: hold the key and speak -> local transcription -> your agent's warm
Claude session streams the reply -> sentences go to the mouth the moment
they complete (~1-2s to first audio on warm turns). The greeting plays
over a hidden warmup query so the first real turn is already hot.

Typing in this terminal is a first-class turn too: same conversation,
spoken reply, and typing while it talks interrupts it.

Flags:
  --open-mic   always-listening mode (VAD endpointing) instead of
               hold-to-talk. Know the tradeoff: room audio (a video,
               music, another voice assistant) can trigger replies to
               speech never meant for the agent.
  --barge-in   with --open-mic: keep listening WHILE speaking.
               HEADPHONES REQUIRED — with open speakers the mic hears
               the reply and the agent interrupts itself.
  --model X    override the model for this session (full id).

Say "goodbye <name>" / "end voice mode" to hang up. Ctrl-C works.
"""
import asyncio
import queue
import sys
import threading
import time

from backtalk import signals
from backtalk.brain import WarmBrain
from backtalk.config import CFG
from backtalk.ears import Ears, record_held, warm as warm_ears
from backtalk.mouth import Mouth
from backtalk.ptt import PTTListener
from backtalk.vlog import log

NAME = CFG["name"]
QUIT_PHRASES = CFG["quit_phrases"]

_PASTE_ON = "\x1b[200~"    # bracketed-paste markers (we enable the mode below)
_PASTE_OFF = "\x1b[201~"


def _clean_typed(line: str) -> str:
    """Scrub terminal-copy artifacts: blockquote gutter glyphs and stray
    whitespace (copying from a CLI chat render drags bars along)."""
    line = line.strip()
    while line[:1] in ("▎", "│", ">"):
        line = line[1:].lstrip()
    return line


def _join_paste(body: str) -> str:
    """Pasted blob -> one clean message (gutters scrubbed, lines joined)."""
    parts = [_clean_typed(l) for l in body.split("\n")]
    return " ".join(" ".join(p for p in parts if p).split())


def _typed_reader_pipe(q: "queue.Queue[str]", fd: int):
    """Non-tty stdin (pipes/tests): line assembly with paste markers."""
    import os
    pend = ""
    while True:
        try:
            b = os.read(fd, 65536)
        except OSError:
            return
        if not b:
            return
        pend += b.decode("utf-8", "replace")
        while True:
            if _PASTE_ON in pend:
                if _PASTE_OFF not in pend:
                    break
                head, rest = pend.split(_PASTE_ON, 1)
                body, pend = rest.split(_PASTE_OFF, 1)
                *hlines, hpart = head.split("\n")
                for l in hlines:
                    l = _clean_typed(l)
                    if l:
                        q.put(l)
                text = _join_paste(hpart + body)
                if text:
                    q.put(text)
                continue
            if "\n" in pend:
                line, pend = pend.split("\n", 1)
                line = _clean_typed(line)
                if line:
                    q.put(line)
                continue
            break


def _typed_reader_simple(q: "queue.Queue[str]"):
    """Windows (no termios): plain line input on a thread. Pastes work;
    they just echo normally instead of collapsing to a count."""
    while True:
        try:
            line = _clean_typed(input())
        except (EOFError, OSError):
            return
        if line:
            q.put(line)


def _typed_reader(q: "queue.Queue[str]"):
    """Terminal stdin -> typed messages (daemon thread). Typed lines are
    first-class turns: same pipeline as a spoken utterance, spoken reply.

    On a POSIX tty we OWN the input line (cbreak: no kernel echo, no
    canonical buffering — the little line editor below echoes keys,
    handles backspace, and assembles bracketed pastes invisibly). The
    kernel's canonical mode is unfixable for pastes: it echoes the
    markers as visible junk and holds unfinished marker lines hostage.
    Pastes show as `[pasted N chars]`; Enter sends everything as ONE
    message. Ctrl-C still works (ISIG stays on); termios restored at
    exit."""
    import atexit
    import os
    fd = sys.stdin.fileno()
    if not os.isatty(fd):
        _typed_reader_pipe(q, fd)
        return
    try:
        import termios
        import tty as _tty
    except ImportError:            # Windows: no termios — simple reader
        _typed_reader_simple(q)
        return
    old = termios.tcgetattr(fd)
    _tty.setcbreak(fd)                      # ECHO+ICANON off, ISIG kept
    sys.stdout.write("\x1b[?2004h")         # bracket pastes, please
    sys.stdout.flush()

    def _restore():
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.flush()
    atexit.register(_restore)

    MARKS = (_PASTE_ON, _PASTE_OFF)

    def _partial_tail(s: str) -> int:
        """Length of a trailing partial paste-marker (hold it for the
        next read)."""
        for m in MARKS:
            for k in range(min(len(s), len(m) - 1), 0, -1):
                if m.startswith(s[-k:]):
                    return k
        return 0

    buf = ""          # the input line being composed
    paste = None      # accumulating paste body, or None
    pend = ""
    while True:
        try:
            b = os.read(fd, 4096)
        except OSError:
            _restore()
            return
        if not b:
            _restore()
            return
        pend += b.decode("utf-8", "replace")
        keep = _partial_tail(pend)
        proc = pend[:len(pend) - keep] if keep else pend
        pend = pend[len(pend) - keep:] if keep else ""
        i = 0
        while i < len(proc):
            if paste is not None:
                j = proc.find(_PASTE_OFF, i)
                if j < 0:
                    paste += proc[i:]
                    break
                paste += proc[i:j]
                i = j + len(_PASTE_OFF)
                text = _join_paste(paste)
                paste = None
                if text:
                    if buf and not buf.endswith(" "):
                        buf += " "
                    buf += text
                    sys.stdout.write(text if len(text) <= 60
                                     else f"[pasted {len(text)} chars]")
                    sys.stdout.flush()
                continue
            if proc.startswith(_PASTE_ON, i):
                paste = ""
                i += len(_PASTE_ON)
                continue
            ch = proc[i]
            i += 1
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                line = buf.strip()
                buf = ""
                if line:
                    q.put(line)
            elif ch in ("\x7f", "\x08"):     # backspace
                if buf:
                    buf = buf[:-1]
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
            elif ch >= " " or ch == "\t":    # printable: echo + collect
                buf += ch
                sys.stdout.write(ch)
                sys.stdout.flush()


async def speak_reply(brain: WarmBrain, mouth: Mouth, text: str):
    """First sentence ships alone (fast start); the rest go in
    2-sentence breaths — fuller chunks get livelier prosody (single
    short sentences come out flat)."""
    t0 = time.time()
    first = True
    batch: list[str] = []

    def emit(raw: str):
        nonlocal first, batch
        # TTS hygiene: backticks, markdown fences and angle-bracket tag
        # syntax are never speakable — the mouth gets clean prose only.
        s = raw.replace("`", "").replace("<<", "").replace(">>", "").strip()
        if not s:
            return
        if first:
            log(f"[{NAME}] ({time.time()-t0:.1f}s to first) {s}")
            mouth.say_chunk(s)
            first = False
        else:
            log(f"[{NAME}] {s}")
            batch.append(s)
            if len(batch) >= 2:
                mouth.say_chunk(" ".join(batch))
                batch = []

    try:
        async for sentence in brain.ask_stream(text):
            emit(sentence)
        if batch:
            mouth.say_chunk(" ".join(batch))
        if first:
            # Zero sentences yielded (brain error / empty turn): nothing
            # will ever dequeue, so nothing resets the bus — park it here.
            signals.static_stop()
            signals.set_state("idle")
    except asyncio.CancelledError:
        try:
            await brain.interrupt()
        except Exception:
            pass
        raise


async def amain():
    open_mic = "--open-mic" in sys.argv
    barge_in = "--barge-in" in sys.argv
    model = None
    if "--model" in sys.argv:
        try:
            model = sys.argv[sys.argv.index("--model") + 1]
        except IndexError:
            pass

    mouth = Mouth()
    ears = Ears()
    brain = WarmBrain(model=model)

    mode = "open-mic" if open_mic else f"push-to-talk ({CFG['ptt_key']})"
    log(f"[backtalk] up — agent={NAME} dir={CFG['agent_dir']} "
        f"model={brain.model} mode={mode} "
        f"(say 'goodbye {NAME.lower()}' to hang up)")
    mouth.say(CFG["greeting"])

    loop = asyncio.get_event_loop()
    # Warm the engines while the greeting plays: the STT model load and
    # the brain's prompt-cache toll both hide behind the spoken line.
    loop.run_in_executor(None, warm_ears)
    await brain.start()
    async for _ in brain.ask_stream(
            "Warmup ping - reply with the single word: ready"):
        pass
    log("[backtalk] brain warm")

    speak_task: asyncio.Task | None = None
    typed_q: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=_typed_reader, args=(typed_q,), daemon=True).start()
    typed_fut: asyncio.Future | None = None

    async def handle(text: str) -> bool:
        """Process one utterance; returns False on quit."""
        nonlocal speak_task
        log(f"[you]    {text}")
        if any(q in text.lower() for q in QUIT_PHRASES):
            if speak_task and not speak_task.done():
                speak_task.cancel()
            mouth.shut_up()
            mouth.say(CFG["signoff"])
            mouth.wait_done(timeout=15)
            return False
        if speak_task and not speak_task.done():
            log("[turn] interrupted mid-reply by new input")
            speak_task.cancel()
            mouth.shut_up()
        if speak_task:
            # Let the cancellation fully land (its brain.interrupt()
            # included) BEFORE anything else touches the brain —
            # otherwise the dead turn's stop signal can race in after
            # the new query and kill the new answer (half of the
            # off-by-one bug; see brain.reset_turn for the other half).
            try:
                await speak_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            speak_task = None
        signals.set_state("thinking")
        signals.static_start()
        # Clean the pipe: drain the interrupted turn's leftovers so the
        # new question can't pair with a stale ResultMessage.
        await brain.reset_turn()
        speak_task = asyncio.create_task(speak_reply(brain, mouth, text))
        return True

    try:
        if open_mic:
            gate = None if barge_in else (lambda: mouth.speaking)
            mic_fut: asyncio.Future | None = None
            while True:
                if mic_fut is None:
                    mic_fut = loop.run_in_executor(
                        None, lambda: ears.listen_once(gate=gate))
                if typed_fut is None:
                    typed_fut = loop.run_in_executor(None, typed_q.get)
                done, _ = await asyncio.wait(
                    {mic_fut, typed_fut},
                    return_when=asyncio.FIRST_COMPLETED)
                if typed_fut in done:
                    text = typed_fut.result(); typed_fut = None
                    if text and not await handle(text):
                        return
                    continue
                text = mic_fut.result(); mic_fut = None
                if text and not await handle(text):
                    return
        else:
            # HOLD-TO-TALK (the default): hold the key -> duck + open
            # mic; release -> close mic + restore + process. The button
            # is the VAD. The mic is CLOSED otherwise (room audio and
            # music can't leak into the transcriber), and pressing while
            # the agent talks interrupts it.
            ptt = PTTListener(CFG["ptt_key"])
            press_fut: asyncio.Future | None = None
            while True:
                if press_fut is None:
                    press_fut = loop.run_in_executor(None, ptt.wait_press)
                if typed_fut is None:
                    typed_fut = loop.run_in_executor(None, typed_q.get)
                done, _ = await asyncio.wait(
                    {press_fut, typed_fut},
                    return_when=asyncio.FIRST_COMPLETED)
                if typed_fut in done:
                    text = typed_fut.result(); typed_fut = None
                    if not await handle(text):
                        return
                    continue
                press_fut.result(); press_fut = None
                if speak_task and not speak_task.done():
                    log("[turn] interrupted mid-reply — key pressed")
                    speak_task.cancel()          # the button = interrupt
                mouth.shut_up()
                signals.static_stop()            # button kills the static too
                signals.set_state("listening")
                mouth.ducker.speech_start()      # duck NOW, while you talk
                print("[ptt] recording (release to send)...", flush=True)
                text = await loop.run_in_executor(
                    None, lambda: record_held(ptt.is_held))
                mouth.ducker.speech_end(0.2)     # snap back fast on release
                if not text:
                    log("[ptt] (tap or empty — ignored)")
                    signals.set_state("idle")
                    continue
                if not await handle(text):
                    return
    except KeyboardInterrupt:
        pass
    finally:
        if speak_task and not speak_task.done():
            speak_task.cancel()
        mouth.shutdown()  # restores the music on Ctrl-C / crash paths too
        signals.static_stop()
        signals.set_state("idle")
        await brain.stop()
        log("[backtalk] hung up")


def main():
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\n[backtalk] interrupted — hanging up", flush=True)


if __name__ == "__main__":
    main()
