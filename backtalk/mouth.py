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
"""The mouth — streaming sentence-chunked TTS, played through one
long-lived output stream.

Default engine: Kokoro, in-process. Local, free, no server, no API key,
~0.2s to first audio once warm. Optional premium engine: ElevenLabs on
YOUR key — read from the system keychain, never from a file (see
_get_elevenlabs_key) — with Kokoro as the automatic fallback: the voice
degrades instead of going mute if the cloud fails.

Sentences are synthesized one at a time and queued for playback, so the
first sentence is audible while later ones are still rendering. Playback
is cancellable mid-word: set the stop event and the speaker goes silent
within one audio block plus the device buffer (~0.15s).

Two threads, not one: a synth thread renders chunks AHEAD of the one
playing (LOOKAHEAD deep), so when a chunk ends the next one's audio is
already sitting there finished and plays with no gap. One thread doing
render-then-play-then-render put a synth-latency-plus-prebuffer pause
(~1s) at every chunk boundary, while the text had long since reached the
screen. A generation counter ties the two threads together for barge-in:
shut_up() bumps it, and every chunk rendered or queued under the old
generation is dropped instead of played.

HARD-WON AUDIO LAW #1 — ONE long-lived OutputStream, reused for every
sentence for the life of the process. A fresh stream per sentence gives
an audible onset blip or a beat of dead air on plenty of audio setups
(USB interfaces, Bluetooth, streaming mixers that latch onto each new
stream late). Proven by A/B test; do not "simplify" this away.

HARD-WON AUDIO LAW #2 — buffer ~0.75s of synthesized audio before a
sentence starts playing, so a slower machine never underruns into
slow-motion garble.
"""
import os
import queue
import re
import shutil
import sys
import tempfile
import threading

import numpy as np
import sounddevice as sd

from backtalk.config import CFG
from backtalk.vlog import log

KOKORO_RATE = 24000
EL_RATE = 44100
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

_pipe = None
_pipe_lock = threading.Lock()


def _ensure_espeak():
    """kokoro phonemizes through system espeak-ng (its bundled loader
    ships a broken build path — found the hard way; upstream's own docs
    say install the system package). Help phonemizer find it in the
    usual homes when the env isn't already set."""
    if os.environ.get("PHONEMIZER_ESPEAK_LIBRARY"):
        return
    candidates = (
        "/opt/homebrew/lib/libespeak-ng.dylib",       # macOS arm64 (brew)
        "/usr/local/lib/libespeak-ng.dylib",          # macOS intel (brew)
        "/usr/lib/x86_64-linux-gnu/libespeak-ng.so.1",  # debian/ubuntu
        "/usr/lib/libespeak-ng.so.1",                 # other linux
        "C:\\Program Files\\eSpeak NG\\libespeak-ng.dll",       # windows
        "C:\\Program Files (x86)\\eSpeak NG\\libespeak-ng.dll",
    )
    for lib in candidates:
        if os.path.exists(lib):
            os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = lib
            break


# Set the moment the TTS pipeline can actually render a word. Anything
# queued before this fires waits ~30 s in silence, and on this machine a
# talk-key press in that window flushes the queue — so the greeting and
# the resume recap were silently destroyed before they ever played
# (2026-08-26). Callers use wait_warm() to hold speech until it is real.
_warm_event = threading.Event()


def is_warm() -> bool:
    """True once the voice can actually speak."""
    return _warm_event.is_set()


def wait_warm(timeout: float | None = None) -> bool:
    """Block until the voice is loaded. False on timeout."""
    return _warm_event.wait(timeout)


def start_warming():
    """Begin loading the voice NOW, in the background, without needing
    something queued first. Cheap to call twice — warm() is idempotent
    under its lock."""
    threading.Thread(target=warm, daemon=True, name="mouth-warm").start()


# Every espeak library filename phonemizer might copy, on any platform. A
# directory holding exactly one of these and nothing else is a phonemizer
# scratch dir and is not plausibly anything else.
_ESPEAK_LIB_NAMES = (
    "espeak-ng.dll",
    "libespeak-ng.dll",
    "libespeak-ng.so",
    "libespeak-ng.so.1",
    "libespeak-ng.dylib",
)


def _is_orphan_espeak_tempdir(path: str) -> bool:
    """True only for a directory whose ENTIRE contents are one espeak
    library. That signature is what makes it safe to point a delete at a
    shared temp folder: one file, and its name is one of five."""
    try:
        entries = os.listdir(path)
    except OSError:
        return False
    return len(entries) == 1 and entries[0] in _ESPEAK_LIB_NAMES


def _sweep_orphan_espeak_tempdirs():
    """Delete espeak scratch dirs left behind by previous runs.

    phonemizer copies the espeak shared library into a fresh temp dir for
    every backend it builds, because espeak-ng keeps its state in globals
    and the loader refuses the same file twice. Kokoro builds several
    backends, so ONE start leaves several behind.

    On POSIX that cleanup rides a finalizer and usually happens. On
    Windows phonemizer can only register it with atexit, and atexit does
    not run when a process is KILLED rather than exited -- so anything
    stopping the voice line by terminating it, which is most launchers and
    every supervisor, leaks every directory it ever made. Sixty had piled
    up on the machine where this was found, and fifteen were sitting on
    the author's own Mac when it was reviewed: the POSIX path is not as
    reliable as it looks either. The count only ever grows.

    Patching phonemizer where it is installed is not a fix, because the
    launcher runs a dependency sync that would overwrite it. Sweeping at
    our own startup bounds the total at one run's worth instead.

    Two things make deleting from a shared temp folder safe, and only the
    first is ours: the signature above is narrow enough that nothing else
    matches it, and anything we are not permitted to remove raises and is
    skipped. On Windows a loaded library cannot be deleted at all, so a
    live instance is protected by the OS rather than by us noticing it.
    POSIX does not work that way, but a process that has already mapped
    the library keeps it after the unlink, so a running instance is
    unharmed either way.
    """
    root = tempfile.gettempdir()
    swept = 0
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path) or not _is_orphan_espeak_tempdir(path):
            continue
        try:
            shutil.rmtree(path)
            swept += 1
        except OSError:
            pass          # in use, or not ours. Leaving it is correct.
    if swept:
        log(f"[mouth] swept {swept} orphaned espeak temp dir(s)")


def warm():
    """Load the Kokoro pipeline (first call downloads the model to the
    HF cache). Called at startup while the greeting text is composed."""
    global _pipe
    with _pipe_lock:
        if _pipe is None:
            _ensure_espeak()
            # Before kokoro makes this run's scratch dirs, clear the ones
            # earlier runs could not clean up on their way out.
            _sweep_orphan_espeak_tempdirs()
            from kokoro import KPipeline
            # The voice name's first letter IS the language pipeline:
            # a=American English, b=British English, e/f/h/i/j/p/z = the
            # other shipped languages. bm_lewis -> 'b'.
            lang = (CFG["voice"] or "bm_lewis")[0]
            log(f"[mouth] loading kokoro (lang '{lang}', "
                f"voice {CFG['voice']})...")
            _pipe = KPipeline(lang_code=lang)
            log("[mouth] voice ready")
        _warm_event.set()
    return _pipe


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_RE.split(text.strip()) if p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def _stream_kokoro(text: str):
    """One sentence -> int16 PCM chunks at 24kHz, in-process."""
    pipe = warm()
    try:
        speed = float(CFG.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    for _, _, audio in pipe(text, voice=CFG["voice"], speed=speed):
        a = np.asarray(audio, dtype=np.float32)
        if a.size:
            yield (np.clip(a, -1.0, 1.0) * 32767).astype(np.int16)


def _stream_elevenlabs(text: str, timeout: float):
    """ElevenLabs -> ffmpeg streaming decode -> int16 PCM at 44.1kHz.

    THE ELEVENLABS DOCTRINE, learned the expensive way:
    - fetch mp3_44100_128 and decode locally (raw 44.1k PCM needs their
      Pro tier; the mp3 decode hides inside network wait anyway)
    - turbo model, stability 0.5, similarity 0.75
    - never the multilingual model for English, never style above 0 —
      both make delivery slow and dull
    - their site previews are MASTERED demo clips; raw API output never
      matches them, so master locally (the ffmpeg chain in config)
    ffmpeg reads stdin as we feed it, so playback still starts before
    synthesis finishes."""
    import subprocess

    import httpx

    el = CFG["elevenlabs"]
    key = _get_elevenlabs_key()
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/"
           f"{el['voice_id']}/stream?output_format=mp3_44100_128")
    proc = subprocess.Popen(
        ["ffmpeg", "-loglevel", "quiet", "-i", "pipe:0",
         "-af", el["master"],
         "-f", "s16le", "-ar", str(EL_RATE), "-ac", "1", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    feed_error: list = []

    def _feed():
        try:
            with httpx.stream("POST", url, headers={"xi-api-key": key},
                              json={"text": text, "model_id": el["model"],
                                    "voice_settings": {
                                        "stability": 0.5,
                                        "similarity_boost": 0.75}},
                              timeout=timeout) as r:
                r.raise_for_status()
                for chunk in r.iter_bytes(chunk_size=4096):
                    proc.stdin.write(chunk)
        except Exception as e:
            feed_error.append(e)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    t = threading.Thread(target=_feed, daemon=True)
    t.start()
    carry = b""
    got_audio = False
    while True:
        data = proc.stdout.read(8820)
        if not data:
            break
        data = carry + data
        usable = len(data) - (len(data) % 2)
        carry = data[usable:]
        if usable:
            got_audio = True
            yield np.frombuffer(data[:usable], dtype=np.int16)
    proc.wait(timeout=10)
    if feed_error and not got_audio:
        raise feed_error[0]


_el_key_cache: str | None = None


def _key_slot() -> str:
    """The credential-store entry name, so someone who already keeps a key
    under their own name points at it instead of storing a second copy."""
    return str(CFG["elevenlabs"].get("key_slot") or "backtalk-elevenlabs")


def _get_elevenlabs_key() -> str:
    """The API key, from the most secure store available — NEVER from a
    file in this repo. Lookup order:
      1. macOS Keychain, item `backtalk-elevenlabs` by default (change it
         with elevenlabs.key_slot) — seed it once with:
         security add-generic-password -a "$USER" -s backtalk-elevenlabs -T /usr/bin/security -w
         (it prompts for the secret; -T lets this code read it without a
         GUI prompt every launch)
      2. Linux secret-tool (libsecret):
         secret-tool store --label backtalk service backtalk-elevenlabs
      3. the ELEVENLABS_API_KEY environment variable — the last-resort
         fallback, and the only option on Windows for now. Know the
         tradeoff: an export line in a shell profile is a plaintext key
         on disk, which is exactly what the keychain path avoids."""
    global _el_key_cache
    if _el_key_cache is not None:
        return _el_key_cache
    import subprocess
    key = ""
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["security", "find-generic-password",
                                "-s", _key_slot(), "-w"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                key = r.stdout.strip()
        elif sys.platform.startswith("linux"):
            from shutil import which
            if which("secret-tool"):
                r = subprocess.run(["secret-tool", "lookup", "service",
                                    _key_slot()],
                                   capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    key = r.stdout.strip()
    except Exception:
        pass
    _el_key_cache = key or os.environ.get("ELEVENLABS_API_KEY", "")
    return _el_key_cache


def _elevenlabs_ready() -> bool:
    el = CFG["elevenlabs"]
    return bool(el.get("enabled") and el.get("voice_id")
                and _get_elevenlabs_key())


def synth_stream(text: str, timeout: float = 30.0):
    """One sentence -> yields (sample_rate, pcm_chunk) as the TTS
    renders. ElevenLabs when configured, Kokoro otherwise — and Kokoro
    as the fallback on ANY ElevenLabs failure. Degrade, never mute."""
    if _elevenlabs_ready():
        try:
            for pcm in _stream_elevenlabs(text, timeout):
                yield EL_RATE, pcm
            return
        except Exception as e:
            log(f"[mouth] elevenlabs failed ({str(e)[:60]}) — "
                f"falling back to {CFG['voice']}")
    for pcm in _stream_kokoro(text):
        yield KOKORO_RATE, pcm


LOOKAHEAD = 2      # chunks rendered ahead of the one playing
_DONE = object()   # end-of-render marker on a _Render's pcm queue


class _Render:
    """One chunk's audio in flight from the synth thread to the player.
    PCM blocks stream through `pcm` as they render (rate is set before
    the first one lands), _DONE closes it. `gen` is the barge-in
    generation the chunk was ordered under; stale means shut_up() has
    happened since and the chunk must never play."""
    __slots__ = ("text", "gen", "pcm", "rate", "directions")

    def __init__(self, text: str, gen: int, directions=None):
        self.text = text
        self.gen = gen
        # the chunk's stage directions, published when its audio starts
        self.directions = directions
        self.pcm: queue.Queue = queue.Queue()
        self.rate: int | None = None


class Mouth:
    def __init__(self):
        from backtalk.ducking import Ducker
        self._q: queue.Queue = queue.Queue()            # (gen, text) in
        self._ready: queue.Queue = queue.Queue(maxsize=LOOKAHEAD)
        self._stop = threading.Event()
        self._speaking = threading.Event()
        # Barge-in generation: shut_up() bumps it, and anything ordered
        # under an older one is dropped wherever it's found. _pending
        # counts current-generation chunks not yet played — "queue
        # empty" stopped meaning "nothing left" once rendering ran ahead
        # of playback, so the speaking flag and wait_done key off this.
        self._gen = 0
        self._pending = 0
        self._lock = threading.Lock()
        # The one persistent output stream (audio law #1).
        # Player-thread-only — never touch from other threads.
        self._out: sd.OutputStream | None = None
        self._out_rate: int | None = None
        self.ducker = Ducker()  # public: PTT ducks for the USER's voice too
        self._synth = threading.Thread(target=self._render_loop, daemon=True)
        self._synth.start()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    @property
    def speaking(self) -> bool:
        return self._speaking.is_set()

    def say(self, text: str):
        """Queue text (split to sentences) for speech."""
        for s in split_sentences(text):
            self._enqueue(s)

    def say_chunk(self, text: str, directions=None):
        """Queue text as ONE TTS request, no sentence splitting — fuller
        chunks get livelier prosody (single short sentences come out
        dull).

        `directions` are the stage directions this chunk carried. They are
        published on the signal bus when this chunk's audio STARTS, which
        is why they travel with it instead of firing at parse time."""
        text = text.strip()
        if text:
            self._enqueue(text, directions)

    def _enqueue(self, text: str, directions=None):
        """MERGED 2026-09-02. Two designs met on this one queue: ours
        carries the barge-in GENERATION and counts what is outstanding so
        the render thread can run ahead of the player; theirs carries the
        chunk's stage DIRECTIONS so they can be published the instant the
        audio starts. Neither is optional, so the item carries all three
        and nothing was dropped."""
        with self._lock:
            self._pending += 1
            self._q.put((self._gen, text, directions or None))

    def shut_up(self):
        """Barge-in: stop current playback and flush everything queued
        or rendering. Bump the generation FIRST, then raise stop: the
        player clears stop and then checks the generation, and that
        order pair is what leaves no window for a dead chunk to play."""
        with self._lock:
            self._gen += 1
            self._pending = 0
        self._stop.set()
        for q_ in (self._q, self._ready):
            try:
                while True:
                    q_.get_nowait()
            except queue.Empty:
                pass

    def shutdown(self):
        """Exit path: stop playback and restore the music SYNCHRONOUSLY
        (the debounced restore timer dies with the process otherwise)."""
        self.shut_up()
        self.ducker.restore_now()

    def wait_done(self, timeout: float | None = None):
        """Block until everything queued has played."""
        import time
        deadline = None if timeout is None else time.time() + timeout
        while self._pending > 0 or self._speaking.is_set():
            time.sleep(0.05)
            if deadline and time.time() > deadline:
                return

    def _render_loop(self):
        """Synth thread: render chunks in order, LOOKAHEAD ahead of the
        player. A render is handed over BEFORE it's filled, so the first
        chunk still starts after the prebuffer rather than after the
        whole render; _ready's bound is what throttles the lookahead."""
        while True:
            gen, text, directions = self._q.get()
            if gen != self._gen:
                continue
            r = _Render(text, gen, directions)
            self._ready.put(r)
            try:
                for rate, pcm in synth_stream(text):
                    if gen != self._gen:
                        break     # barged in: don't finish a dead chunk
                    r.rate = rate
                    r.pcm.put(pcm)
            except Exception as e:
                log(f"[mouth] synth error: {e}")
            finally:
                r.pcm.put(_DONE)

    def _run(self):
        from backtalk import signals
        while True:
            r = self._ready.get()
            self._stop.clear()
            if r.gen != self._gen:
                continue
            self._speaking.set()
            self.ducker.speech_start()
            signals.static_stop()     # thinking sound dies when speech starts
            signals.set_state("speaking")
            try:
                self._play_render(r)
            except Exception as e:
                log(f"[mouth] synth/play error: {e}")
            finally:
                with self._lock:
                    if r.gen == self._gen:
                        self._pending -= 1
                    done = self._pending <= 0
                if done:
                    self._speaking.clear()
                    # The reply has genuinely stopped talking, as opposed to
                    # the gap between two sentences of the same reply.
                    signals.reply_done()
                    self.ducker.speech_end()
                    signals.set_state("idle")

    def _get_out(self, rate: int) -> sd.OutputStream:
        """The long-lived stream (audio law #1). Reopened only when the
        sample rate changes (ElevenLabs 44.1k <-> Kokoro 24k fallback:
        rare, costs at most one blip on the switch)."""
        if self._out is not None and self._out_rate == rate:
            # Guarded, because the stream can die UNDER us: the ears
            # rebuild the whole audio system to recover from a device
            # change (see ears._reopen_after_device_change), and that
            # closes every open stream including this one. Touching a
            # dead stream raises rather than returning False, so the
            # check has to be the try, not an `if`. Falling through
            # rebuilds it, which is what the rest of this method does.
            try:
                if not self._out.active:
                    self._out.start()
                return self._out
            except Exception:
                log("[mouth] the output stream went away, reopening")
        self._drop_out()
        self._out = sd.OutputStream(samplerate=rate, channels=1, dtype="int16")
        self._out_rate = rate
        self._out.start()
        return self._out

    def _cut(self):
        """Barge-in cut: stop feeding audio and pad the line with a beat
        of silence — the stream itself NEVER stops (an abort+restart here
        re-triggers the onset blip on latch-happy audio setups). Cost:
        the device buffer (~0.1s) plays out after the kill order — half a
        syllable of tail."""
        try:
            zeros = np.zeros(2205, dtype=np.int16)
            for _ in range(3):
                self._out.write(zeros)
        except Exception:
            self._drop_out()

    def _drop_out(self):
        """Close and forget the stream — the next sentence reopens
        fresh. The self-heal path for device errors (interface
        unplugged, audio mixer restarted)."""
        if self._out is not None:
            try:
                self._out.close(ignore_errors=True)
            except Exception:
                pass
        self._out = None
        self._out_rate = None

    def _play_render(self, r: _Render, block: int = 2205,
                     prebuffer_s: float = 0.75):
        """Play one render as it fills, with the head-start buffer (audio
        law #2) — already satisfied instantly for any chunk that finished
        rendering while the previous one played. stop() reacts ~50ms.
        The sample rate comes from whichever engine actually answered."""
        from backtalk import signals
        head: list = []
        banked = 0
        finished = False
        while True:
            pcm = r.pcm.get()
            if pcm is _DONE:
                finished = True
                break
            head.append(pcm)
            banked += len(pcm)
            if banked >= int(r.rate * prebuffer_s):
                break
        if not head:
            return
        try:
            out = self._get_out(r.rate)
            # AUDIO STARTS HERE: the head buffer is full and the first write
            # is next. Publishing now is what puts a screen cue on the spoken
            # word rather than seconds ahead of it. THEIRS, kept whole - only
            # the source of the directions changed, because on this branch a
            # chunk reaches the player as a _Render rather than as a bare
            # sentence, so they travel on the render itself.
            if r.directions:
                from backtalk import signals as _sig
                _sig.direction(r.directions)

            def _write(pcm):
                for i in range(0, len(pcm), block):
                    if self._stop.is_set():
                        return False
                    out.write(pcm[i:i + block])
                    # Re-check after the blocking write: a barge-in
                    # landing mid-block must not let feed_waveform
                    # re-assert "speaking" over a fresh "listening".
                    if self._stop.is_set():
                        return False
                    signals.feed_waveform(pcm[i:i + block])
                return True
            for pcm in head:
                if not _write(pcm):
                    self._cut()
                    return
            while not finished:
                pcm = r.pcm.get()
                if pcm is _DONE:
                    break
                if not _write(pcm):
                    self._cut()
                    return
        except Exception:
            self._drop_out()
            raise


if __name__ == "__main__":
    m = Mouth()
    m.say(sys.argv[1] if len(sys.argv) > 1 else
          "Voice check. The mouth is alive, and it is very good to be heard.")
    m.wait_done(timeout=60)
