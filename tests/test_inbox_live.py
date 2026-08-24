# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inbox + stdin share ONE queue, in order.

What this proves: the inbox (a socket producer) and the terminal's
stdin reader (a direct `q.put()`) both land on the same queue.Queue,
and a consumer that drains it one item at a time — the shape of
amain()'s loop — sees every message exactly once, in the order it was
sent, even when the two producers interleave.

What this does NOT prove: real concurrency, or serialization against
the live voice loop. There is no consumer running while messages are
sent, and no overlapping handle() calls — a single-threaded drain
after the fact is a queue.Queue stdlib guarantee, not a demonstration
of the running system. End-to-end serialization against a live voice
session (a spoken turn in flight while a typed one arrives) is
verified manually, not by this script.

Run:  .venv/Scripts/python tests/test_inbox_live.py
"""
import json
import queue
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtalk import inbox
from backtalk.config import CFG


def free_port():
    """Ask the OS for a currently-unused loopback port, then let it go.
    Never a hardcoded port — a real session could be listening on one of
    those, and stealing it would silently eat someone's typed turns."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# the config key must exist and default to off
assert "inbox_port" in CFG, "config.DEFAULTS is missing inbox_port"

q = queue.Queue()
port_wanted = free_port()
port = inbox.start(q, port_wanted)
assert port == port_wanted

def send(text):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall((json.dumps({"text": text}) + "\n").encode("utf-8"))
    s.makefile("r", encoding="utf-8").readline()
    s.close()

# interleave the two producers the way a real session would
send("typed from the GUI")
q.put("typed at the terminal")          # what _typed_reader does
send("second from the GUI")

drained = []
while not q.empty():
    drained.append(q.get())

assert drained == ["typed from the GUI",
                   "typed at the terminal",
                   "second from the GUI"], drained

print("test_inbox_live: OK")
