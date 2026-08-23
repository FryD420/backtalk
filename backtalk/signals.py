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
"""The signal bus — tiny files any other program can watch.

The voice line leaves notes; faces read the notes. That one dumb trick
is the whole integration surface:

  .voice_state        idle | listening | thinking | speaking
  .voice_waveform     JSON {ts, samples: [64 floats]} while audio plays
  .voice_loading_pid  exists while the thinking sound is playing
  .voice_heartbeat    unix time as text, rewritten every ~2 s while the
                      voice line is alive (stale or missing = dead/hung)
  .voice_activity     JSON {ts, turn_started, line} — what the agent is
                      doing RIGHT NOW during a turn ("Read: foo.py");
                      deleted when the turn ends

Written to signals_dir (default: the repo root). Visualizers built on
this contract just work. The heartbeat and activity files are how a face
tells "thinking hard" from "process dead": state says thinking either
way, only the heartbeat keeps ticking in the first case.

THE BAREHANDS SEAM: set barehands_state_dir in backtalk.json to a
barehands checkout's state/ folder and the same signals are mirrored in
its format (state/state as a bare word, state/wave.json normalized
0..1) — the on-screen ring becomes your agent's face with zero glue.

Every write is wrapped: the bus must never crash the voice line.
"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time

import numpy as np

from backtalk.config import CFG

_DIR = CFG["signals_dir"]
_STATE_FILE = os.path.join(_DIR, ".voice_state")
_WAVEFORM_FILE = os.path.join(_DIR, ".voice_waveform")
_LOADING_PID_FILE = os.path.join(_DIR, ".voice_loading_pid")
_HEARTBEAT_FILE = os.path.join(_DIR, ".voice_heartbeat")
_ACTIVITY_FILE = os.path.join(_DIR, ".voice_activity")

_BH = CFG.get("barehands_state_dir") or ""
_BH_STATE = os.path.join(_BH, "state") if _BH else ""
_BH_WAVE = os.path.join(_BH, "wave.json") if _BH else ""

_THINKING_SOUND = CFG.get("thinking_sound") or ""

_WAVEFORM_MIN_INTERVAL = 1.0 / 15   # ~15 writes/sec is plenty for 60fps reads
_last_waveform_write = 0.0
_static_proc: subprocess.Popen | None = None


def set_state(name: str):
    """Write the state. Never raises — the show must go on."""
    try:
        with open(_STATE_FILE, "w") as f:
            f.write(name)
    except OSError:
        pass
    if _BH_STATE:
        try:
            with open(_BH_STATE, "w") as f:
                f.write(name)
        except OSError:
            pass


def feed_waveform(pcm: np.ndarray):
    """Feed one PCM block (int16) — throttled, downsampled to 64 points.

    Also re-asserts state="speaking" on the same throttle: this only runs
    while the mouth is audibly playing, so the bus self-heals within
    ~70ms if a stray writer stomps the state mid-speech. (That self-heal
    rule once closed a bug that took a whole evening to find.)"""
    global _last_waveform_write
    if pcm.size == 0:
        return
    now = time.time()
    if now - _last_waveform_write < _WAVEFORM_MIN_INTERVAL:
        return
    _last_waveform_write = now
    try:
        idx = np.linspace(0, pcm.size - 1, 64).astype(int)
        raw = pcm[idx].astype(float)
        with open(_WAVEFORM_FILE, "w") as f:
            f.write(json.dumps({"ts": now, "samples": raw.tolist()}))
        if _BH_WAVE:
            norm = np.clip(np.abs(raw) / 32768.0, 0.0, 1.0)
            with open(_BH_WAVE, "w") as f:
                f.write(json.dumps({"ts": now, "samples": norm.tolist()}))
    except (OSError, ValueError):
        pass
    set_state("speaking")


def _player_cmd(path: str) -> list[str] | None:
    if sys.platform == "darwin":
        return ["afplay", "-v", "0.35", path]
    for cand in ("ffplay", "aplay", "paplay"):
        from shutil import which
        if which(cand):
            if cand == "ffplay":
                return ["ffplay", "-nodisp", "-autoexit", "-loglevel",
                        "quiet", "-volume", "35", path]
            return [cand, path]
    return None


def static_start():
    """Optional thinking sound — plays while the brain works."""
    global _static_proc
    if not _THINKING_SOUND or not os.path.exists(_THINKING_SOUND):
        return
    static_stop()
    cmd = _player_cmd(_THINKING_SOUND)
    if not cmd:
        return
    try:
        _static_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(_LOADING_PID_FILE, "w") as f:
            f.write(str(_static_proc.pid))
    except OSError:
        _static_proc = None


def static_stop():
    global _static_proc
    if _static_proc is not None:
        try:
            _static_proc.terminate()
        except OSError:
            pass
        _static_proc = None
    try:
        os.remove(_LOADING_PID_FILE)
    except OSError:
        pass


# ---- THE HEARTBEAT: .voice_heartbeat
# Content: the current unix time as text (e.g. "1787702400.12"), rewritten
# every ~2 s for as long as the voice line is alive. A face that sees it
# older than a few seconds (ai-visualizer uses 6) shows the link as LOST.
# It rides the asyncio loop ON PURPOSE: a loop wedged in a sync call
# stops beating, so "hung" reads as dead — that is the signal we want.
# Falls back to a daemon thread only if no loop is running. Removed on
# heartbeat_stop() so a clean exit reads LOST at once, not 6 s later.
_HEARTBEAT_INTERVAL = 2.0
_hb_task: asyncio.Task | None = None
_hb_thread: threading.Thread | None = None
_hb_stop_evt = threading.Event()


def _beat():
    try:
        with open(_HEARTBEAT_FILE, "w") as f:
            f.write(f"{time.time():.2f}")
    except OSError:
        pass


async def _heartbeat_loop():
    try:
        while True:
            _beat()
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
    except asyncio.CancelledError:
        pass


def _heartbeat_thread():
    while not _hb_stop_evt.wait(_HEARTBEAT_INTERVAL):
        _beat()


def heartbeat_start():
    """Start the pulse. Never raises; calling it twice is a no-op."""
    global _hb_task, _hb_thread
    if (_hb_task and not _hb_task.done()) or \
            (_hb_thread and _hb_thread.is_alive()):
        return
    _beat()
    try:
        _hb_task = asyncio.get_running_loop().create_task(
            _heartbeat_loop())
    except RuntimeError:                     # no loop here: thread it
        _hb_stop_evt.clear()
        _hb_thread = threading.Thread(target=_heartbeat_thread,
                                      daemon=True)
        _hb_thread.start()
    except Exception:
        pass


def heartbeat_stop():
    """Stop the pulse and remove the file. Never raises."""
    global _hb_task, _hb_thread
    if _hb_task is not None:
        try:
            _hb_task.cancel()
        except Exception:
            pass
        _hb_task = None
    if _hb_thread is not None:
        _hb_stop_evt.set()
        _hb_thread = None
    for path in (_HEARTBEAT_FILE, _ACTIVITY_FILE):
        try:
            os.remove(path)
        except OSError:
            pass


# ---- THE ACTIVITY LINE: .voice_activity
# Content: JSON {"ts": <unix float>, "turn_started": <unix float>,
#                "line": "<short text>"}
# Exists only while a turn is in flight. turn_begin() opens it (line
# "working"), activity("Read: foo.py") rewrites the line per tool call
# keeping turn_started, turn_end() deletes it. A face shows the line as
# a ticker and turn_started as an elapsed clock; the server-side age of
# ts says how long the agent has sat on one tool call.
_turn_started = 0.0


def _write_activity(line: str):
    try:
        with open(_ACTIVITY_FILE, "w") as f:
            f.write(json.dumps({"ts": time.time(),
                                "turn_started": _turn_started,
                                "line": line}))
    except (OSError, ValueError, TypeError):
        pass


def turn_begin():
    """A user turn starts: reset the elapsed clock, open the file."""
    global _turn_started
    _turn_started = time.time()
    _write_activity("working")


def activity(line: str):
    """What the agent is doing right now (one short line, no prefix).
    Outside a turn (warmup, resume recap) the clock starts here."""
    global _turn_started
    if not _turn_started:
        _turn_started = time.time()
    line = " ".join(str(line or "").split())[:120]
    _write_activity(line)


def turn_end():
    """The turn is over (answered, cancelled, or failed): clear it."""
    global _turn_started
    _turn_started = 0.0
    try:
        os.remove(_ACTIVITY_FILE)
    except OSError:
        pass
