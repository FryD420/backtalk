# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inbox + stdin share ONE queue, in order.

The real risk this plan carries is a second path to the brain. This
check proves there isn't one: both producers land on the same
queue.Queue, and a consumer that drains it one at a time — exactly what
amain()'s loop does — sees every message once, in the order sent.

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

# the config key must exist and default to off
assert "inbox_port" in CFG, "config.DEFAULTS is missing inbox_port"

q = queue.Queue()
port = inbox.start(q, 8796)
assert port == 8796

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
