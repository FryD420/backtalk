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
"""Session log — terminal print + timestamped append to logs/backtalk.log.

Exists because the hardest voice bug ever hit here (the off-by-one
interrupt desync) had to be diagnosed from source, because the session
only printed to a terminal window nobody saved. Every load-bearing line
([you], replies, interrupts, drain/rebuild events, TTS fallbacks) goes
through log() so the next gremlin comes with receipts.
"""
import datetime
import os
import sys
from pathlib import Path

_LOGS = Path(__file__).resolve().parent.parent / "logs"


def _resolve_log_path() -> Path:
    """Pick the log file, keeping the TEST suite out of the real one.

    The production log is a diagnostic record, not a scratch file — this
    module exists because a bug once had to be reconstructed from it. But
    the test suite imports the same modules the voice line does, so running
    it appended fake gate cases and lines like "no brain at all" straight
    into logs/backtalk.log. The receipts then read as though real launches
    had failed, which is worse than having no log: it actively misleads
    whoever is chasing the next gremlin.

    Two ways out, in priority order:

    1. BACKTALK_LOG, an explicit path override. For anything that wants its
       own log — a harness, a one-off repro, a second instance.
    2. argv[0] sitting in tests/ — every test here is run as
       `python tests/test_x.py`, so this catches a directly-run test with no
       edits to the test files themselves. That matters: their import
       preludes all differ, so a per-file fix would have been eleven
       inconsistent edits, and the twelfth test written would forget.

    Deliberately a heuristic, and deliberately a safe one: if the sniff ever
    misses, the only cost is a test writing the production log — exactly
    today's behaviour, never a lost or misdirected production line.
    """
    override = os.environ.get("BACKTALK_LOG")
    if override:
        return Path(override)
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        try:
            if Path(argv0).resolve().parent.name == "tests":
                return _LOGS / "backtalk-test.log"
        except OSError:
            pass  # unresolvable argv0 is not a reason to lose logging
    return _LOGS / "backtalk.log"


LOG_PATH = _resolve_log_path()


def _init_console():
    """Ask a Windows console for UTF-8 before anything is printed at it.

    Windows consoles default to a legacy codepage (cp1252 on a UK/US
    install), so a UTF-8 em-dash arrives as mojibake: the startup banner
    rendered as "[backtalk] up a<TM>" instead of "up --". Fixing the
    banner's own characters would not have been a fix, because the
    agent's REPLIES are printed here too and can contain anything at all.

    errors="replace" on the streams means a character the terminal
    genuinely cannot draw degrades to "?" rather than raising mid
    sentence and taking the voice down. No-ops everywhere but Windows.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_init_console()


def log(line: str):
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Last resort if the console refused UTF-8: readable beats fatal.
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # encoding pinned on purpose. The default is the platform's, which
        # on Windows is that same legacy codepage -- so the log file kept
        # its own permanently corrupted copy of every line the console had
        # already mangled, and the receipts this module exists to produce
        # were unreadable exactly where they were most needed.
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except Exception:
        pass  # a broken log file must never take the voice down
