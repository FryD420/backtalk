# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Talk-key matching, no keyboard hook needed: the listener class is stubbed
so nothing installs a global hook, and key events are fed straight into the
callbacks pynput would have called.

The bug this pins (found on a Windows laptop, 2026-09-04): pynput reports
the right Alt key as Key.alt_gr on Windows, because alt_r and alt_gr share
one virtual-key code (VK_RMENU, 165) and the listener's vk lookup table
keeps whichever enum member was defined last. resolve_key("right_alt")
hands back Key.alt_r, the two are distinct enum members, and a strict !=
in _on_press threw every press away -- a healthy voice line that never
fires, which is exactly the failure class ptt.py's own docstring warns
about. Confirmed independently on a second Windows machine the same day.

Asserts:
  1. resolve_key still maps the friendly names as documented
  2. the configured key's own object is matched (nothing regressed)
  3. the SAME physical key under pynput's other name is matched (the bug),
     in both directions
  4. a different key with a different vk is NOT matched (no over-match)
  5. release goes through the same matcher and settles after the grace
  6. single-character keys still match by character, and only that one

Run:  .venv/Scripts/python tests/test_ptt_key.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pynput import keyboard

from backtalk import ptt


class _NoHook:
    """Stands in for pynput.keyboard.Listener so this test never installs a
    global keyboard hook. PTTListener only ever sets .daemon and calls
    start() on it."""
    def __init__(self, on_press=None, on_release=None):
        self.daemon = False

    def start(self):
        pass


# ptt did `from pynput import keyboard`, so this swaps the class on the
# shared module object -- fine for a one-process test script.
ptt.keyboard.Listener = _NoHook


def listener(key):
    return ptt.PTTListener(key)


# ---- 1. friendly names, unchanged
assert ptt.resolve_key("right_alt") is keyboard.Key.alt_r
assert ptt.resolve_key("home") is keyboard.Key.home
assert ptt.resolve_key("a") == keyboard.KeyCode.from_char("a")

# ---- 2. the configured object itself still matches
p = listener("right_alt")
p._on_press(keyboard.Key.alt_r)
assert p.is_held(), "the configured key object must still count"

p = listener("home")
p._on_press(keyboard.Key.home)
assert p.is_held()

# ---- 3. THE BUG: same physical key, pynput's other name for it
vk_r, vk_gr = keyboard.Key.alt_r.value.vk, keyboard.Key.alt_gr.value.vk
assert vk_r == vk_gr, f"premise: alt_r/alt_gr share a vk here ({vk_r} vs {vk_gr})"
assert keyboard.Key.alt_r != keyboard.Key.alt_gr, "premise: distinct enum members"

p = listener("right_alt")
p._on_press(keyboard.Key.alt_gr)
assert p.is_held(), ("right Alt arrives as Key.alt_gr on Windows (same vk as "
                     "alt_r) and must count as the talk key")

p = listener("alt_gr")            # and the other way round
p._on_press(keyboard.Key.alt_r)
assert p.is_held(), "a config of alt_gr must accept the OS calling it alt_r"

# ---- 4. no over-match: a different key is still a different key
p = listener("right_alt")
p._on_press(keyboard.Key.alt_l)
assert not p.is_held(), "left Alt has its own vk and must not fire the talk key"
p._on_press(keyboard.Key.ctrl_r)
assert not p.is_held()
p._on_press(None)                 # pynput can deliver None for unknown keys
assert not p.is_held()

# ---- 5. release path uses the same matcher and settles after the grace
p = listener("right_alt")
p._on_press(keyboard.Key.alt_gr)
assert p.is_held()
p._on_release(keyboard.Key.alt_gr)
time.sleep(p.RELEASE_GRACE + 0.05)
assert not p.is_held(), "a settled release under the other name must let go"

# ---- 6. single characters: by character, and only that one
p = listener("a")
p._on_press(keyboard.KeyCode(vk=65, char="a"))   # what the listener delivers
assert p.is_held()
p = listener("a")
p._on_press(keyboard.KeyCode(vk=66, char="b"))
assert not p.is_held()

print("test_ptt_key: all checks passed")
