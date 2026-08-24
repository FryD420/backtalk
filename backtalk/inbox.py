# backtalk: talk to your Claude Code agent out loud.
# Copyright (C) 2026 Jared Rhodenizer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The inbox — a typed message from any local program becomes a turn.

backtalk's main loop already treats a typed line as a first-class turn:
the stdin reader puts strings on typed_q and the loop hands each one to
handle(), the same path a spoken utterance takes. This module is simply
a SECOND producer for that queue, reachable over loopback, so a GUI can
type into the live voice conversation instead of starting a sibling one.

Serialization is inherited, not implemented: the loop dequeues one item
at a time and awaits handle() before taking the next. NOTHING here may
touch the brain directly — a parallel path re-triggers the off-by-one
desync documented in brain.reset_turn().

Protocol: JSON Lines over TCP, one object per line, UTF-8.
  ->  {"text": "what's the status?"}
  <-  {"ok": true}                      or  {"ok": false, "error": "..."}
Multi-line text collapses into one message, so a pasted block is one turn.

Loopback only, by construction, and off unless inbox_port is set.
"""
import os
import json
import queue
import socket
import threading

from backtalk.typed import clean_typed, join_paste

MAX_LINE = 256 * 1024        # a generous paste; anything bigger is junk
MAX_CONNS = 8                # one local GUI, not a web server; 8 is plenty


def handle_payload(raw: str):
    """Parse one request line. Returns (text, None) or (None, error).
    Pure — no sockets, no queue — so the protocol is testable alone."""
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None, "not valid JSON"
    if not isinstance(obj, dict):
        return None, "expected a JSON object"
    text = obj.get("text")
    if not isinstance(text, str):
        return None, 'missing string field "text"'
    text = join_paste(text) if "\n" in text else clean_typed(text)
    if not text:
        return None, "empty message"
    return text, None


def _serve_conn(conn, q, log, sem):
    """One connection: read request lines until the peer goes away."""
    try:
        conn.settimeout(60)
        with conn, conn.makefile("rwb") as f:
            while True:
                line = f.readline(MAX_LINE)
                if not line:
                    return
                raw = line.decode("utf-8", "replace").strip()
                if not raw:
                    continue
                text, err = handle_payload(raw)
                if err:
                    reply = {"ok": False, "error": err}
                else:
                    q.put(text)
                    log(f"[inbox] {text[:120]}")
                    reply = {"ok": True}
                f.write((json.dumps(reply) + "\n").encode("utf-8"))
                f.flush()
    except Exception:
        return          # a broken client must never reach the voice line
    finally:
        sem.release()


def _accept_loop(srv, q, log, sem):
    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            return
        # Defence in depth: we bound loopback, but never read a peer
        # that somehow is not one.
        if not str(addr[0]).startswith("127."):
            try:
                conn.close()
            except OSError:
                pass
            continue
        if not sem.acquire(blocking=False):
            log("[inbox] connection limit reached, rejecting")
            try:
                reply = {"ok": False, "error": "connection limit reached"}
                conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
            continue
        try:
            threading.Thread(target=_serve_conn, args=(conn, q, log, sem),
                             daemon=True).start()
        except Exception as e:
            # The thread never got a chance to run, so nobody else will
            # release this permit or close this socket — do it here, or
            # both leak and the accept loop dies silently besides.
            sem.release()
            try:
                conn.close()
            except OSError:
                pass
            log(f"[inbox] failed to start connection thread ({e})")


def start(q: "queue.Queue[str]", port: int, log=print, max_conns=MAX_CONNS):
    """Bind 127.0.0.1:port and serve on a daemon thread. Returns the
    bound port, or None when disabled or unavailable. Never raises."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    if port <= 0:
        return None
    sem = threading.Semaphore(max_conns)
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR and Windows do not mix: on Windows it lets an
        # unrelated process bind a port we are already listening on and
        # steal every connection meant for us, instead of merely easing
        # TIME_WAIT reuse like it does on POSIX. SO_EXCLUSIVEADDRUSE is
        # the Windows opposite number — it makes a bind-to-our-port
        # attempt from another process fail, which is what we want here.
        if os.name == "nt":
            excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if excl is not None:
                srv.setsockopt(socket.SOL_SOCKET, excl, 1)
        else:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(max_conns)
    except OSError as e:
        log(f"[inbox] could not listen on 127.0.0.1:{port} ({e}) — "
            f"typed input from other programs is off this session")
        return None
    try:
        threading.Thread(target=_accept_loop, args=(srv, q, log, sem),
                         daemon=True).start()
        log(f"[inbox] listening on 127.0.0.1:{port}")
    except Exception as e:
        try:
            srv.close()
        except OSError:
            pass
        log(f"[inbox] failed to start listener thread ({e}) — "
            f"typed input from other programs is off this session")
        return None
    return port
