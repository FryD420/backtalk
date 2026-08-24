# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inbox listener check, no voice session needed: a bare queue stands in
for backtalk's typed_q and a real loopback socket carries the traffic.

Asserts:
  1. the payload parser accepts a good line and rejects junk
  2. a multi-line message arrives as ONE queue item, not several
  3. an end-to-end socket round trip puts the text on the queue and
     acknowledges it
  4. port 0 in config means the listener never binds
  5. two messages on one connection arrive in order

Run:  .venv/Scripts/python tests/test_inbox.py
"""
import json
import queue
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtalk import inbox

# ---- 1. the pure parser
text, err = inbox.handle_payload('{"text": "hello boss"}')
assert err is None, err
assert text == "hello boss"

text, err = inbox.handle_payload('not json at all')
assert text is None and err is not None

text, err = inbox.handle_payload('{"nope": 1}')
assert text is None and err is not None

text, err = inbox.handle_payload('{"text": "   "}')
assert text is None and err is not None, "blank must not become a turn"

# ---- 2. multi-line collapses to ONE message
text, err = inbox.handle_payload(json.dumps({"text": "line one\nline two"}))
assert err is None
assert text == "line one line two", text

# ---- 4. disabled by default
q0 = queue.Queue()
assert inbox.start(q0, 0) is None, "port 0 must not bind"

# ---- 3 & 5. end to end
q = queue.Queue()
port = inbox.start(q, 8795)
assert port == 8795, port

def send(obj):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall((json.dumps(obj) + "\n").encode("utf-8"))
    reply = s.makefile("r", encoding="utf-8").readline()
    s.close()
    return json.loads(reply)

assert send({"text": "first message"})["ok"] is True
assert send({"text": "second message"})["ok"] is True
assert send({"text": ""})["ok"] is False

got = [q.get(timeout=5), q.get(timeout=5)]
assert got == ["first message", "second message"], got
assert q.empty(), "the blank message must not have been queued"

# two messages down ONE connection, order preserved
s = socket.create_connection(("127.0.0.1", port), timeout=5)
f = s.makefile("r", encoding="utf-8")
s.sendall((json.dumps({"text": "alpha"}) + "\n").encode("utf-8"))
s.sendall((json.dumps({"text": "beta"}) + "\n").encode("utf-8"))
assert json.loads(f.readline())["ok"] is True
assert json.loads(f.readline())["ok"] is True
s.close()
assert [q.get(timeout=5), q.get(timeout=5)] == ["alpha", "beta"]

print("test_inbox: OK")
