# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One shared config in git; one small file per machine beside it.

Two machines share backtalk.json through a private repo. Almost all of
it is portable --- name, model, voice, ports. What is NOT portable is a
folder that exists on one machine and not the other.

The overlay holds only that. It is MERGED over the shared config, never
substituted for it, so a machine that forgets to write one still gets a
complete, correct config instead of an empty one.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtalk import config as C      # noqa: E402

failures = []


def check(label, cond):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def load_with(shared, local):
    """Run config.load() against temp files, restoring the real paths after."""
    tmp = tempfile.mkdtemp()
    sp = os.path.join(tmp, "backtalk.json")
    lp = os.path.join(tmp, "backtalk.local.json")
    with open(sp, "w") as f:
        json.dump(shared, f)
    if local is not None:
        with open(lp, "w") as f:
            json.dump(local, f)
    old_c, old_l = C.CONFIG_PATH, C.LOCAL_CONFIG_PATH
    try:
        C.CONFIG_PATH = __import__("pathlib").Path(sp)
        C.LOCAL_CONFIG_PATH = __import__("pathlib").Path(lp)
        return C.load()
    finally:
        C.CONFIG_PATH, C.LOCAL_CONFIG_PATH = old_c, old_l


def main():
    print("machine-local overlay")

    shared = {"name": "Jarvis", "voice": "bm_george", "extra_dirs": ["A"]}

    cfg = load_with(shared, None)
    check("no overlay: the shared config is used whole",
          cfg["name"] == "Jarvis" and cfg["voice"] == "bm_george")

    cfg = load_with(shared, {})
    check("an empty overlay changes nothing",
          cfg["name"] == "Jarvis" and cfg["extra_dirs"] == ["A"])

    cfg = load_with(shared, {"extra_dirs": ["B", "C"]})
    check("the overlay wins on the key it names",
          cfg["extra_dirs"] == ["B", "C"])
    check("keys the overlay does NOT name survive from shared",
          cfg["name"] == "Jarvis" and cfg["voice"] == "bm_george")

    cfg = load_with(shared, {"mic_device": "Some Other Mic"})
    check("the overlay can add a key the shared config omits",
          cfg["mic_device"] == "Some Other Mic")

    # A broken overlay must not take the voice line down with it.
    tmp = tempfile.mkdtemp()
    sp = os.path.join(tmp, "backtalk.json")
    lp = os.path.join(tmp, "backtalk.local.json")
    with open(sp, "w") as f:
        json.dump(shared, f)
    with open(lp, "w") as f:
        f.write("{ this is not json")
    old_c, old_l = C.CONFIG_PATH, C.LOCAL_CONFIG_PATH
    try:
        C.CONFIG_PATH = __import__("pathlib").Path(sp)
        C.LOCAL_CONFIG_PATH = __import__("pathlib").Path(lp)
        cfg = C.load()
        check("a malformed overlay is survivable", cfg["name"] == "Jarvis")
    except Exception as e:
        check("a malformed overlay is survivable (raised %s)" % e, False)
    finally:
        C.CONFIG_PATH, C.LOCAL_CONFIG_PATH = old_c, old_l

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        for f in failures:
            print("  - " + f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
