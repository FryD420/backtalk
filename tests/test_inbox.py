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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtalk import inbox


def free_port():
    """Ask the OS for a currently-unused loopback port, then let it go.
    Never a hardcoded port — a real session could be listening on one of
    those, and stealing it would silently eat someone's typed turns."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port

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
port_wanted = free_port()
port = inbox.start(q, port_wanted)
assert port == port_wanted, port

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

# ---- 6. connection cap and recovery
# The cap is per-listener (start()'s max_conns kwarg), not a module
# global, so a smaller limit for this check never affects any other
# listener in this process.
q_cap = queue.Queue()
port_cap_wanted = free_port()
port_cap = inbox.start(q_cap, port_cap_wanted, max_conns=2)
assert port_cap == port_cap_wanted, port_cap

# Open 2 connections and keep them open (don't close)
conns = []
for i in range(2):
    s = socket.create_connection(("127.0.0.1", port_cap), timeout=2)
    conns.append(s)

# Try a 3rd connection; it should be rejected
s3 = socket.create_connection(("127.0.0.1", port_cap), timeout=2)
f3 = s3.makefile("r", encoding="utf-8")
try:
    reply = f3.readline()
    # Connection is rejected with error response
    if reply:
        assert json.loads(reply)["ok"] is False
    else:
        # Or it's closed immediately (empty read)
        pass
except (ConnectionResetError, OSError):
    # Connection closed by server, which is also valid rejection
    pass
finally:
    s3.close()

# Close the first two connections, freeing up permits
for s in conns:
    s.close()

# Now a new connection should work and deliver its message
s4 = socket.create_connection(("127.0.0.1", port_cap), timeout=2)
s4.sendall((json.dumps({"text": "after recovery"}) + "\n").encode("utf-8"))
reply4 = s4.makefile("r", encoding="utf-8").readline()
s4.close()
assert json.loads(reply4)["ok"] is True
assert q_cap.get(timeout=2) == "after recovery"

print("test_inbox: OK")
