# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Paths in backtalk.json must survive moving to another machine.

The stack was nailed to E:\\my-agent. A second machine has no E: drive,
so every path in the shared config has to resolve per machine instead of
naming a drive letter. `~` alone is not enough: the agent folder and the
vault do not live under the user profile.

An UNRESOLVED variable is the dangerous case. os.path.expandvars leaves
%NOPE% untouched, which would otherwise sail on as a literal folder name
and fail somewhere far away with a confusing message. It is reported
here, at the source, and never raises -- a bad path must not kill the
voice line on startup.
"""
import io
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtalk.config import _expand      # noqa: E402

failures = []


def check(label, cond):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def main():
    print("path expansion")

    os.environ["JARVIS_TEST_ROOT"] = r"C:\somewhere\agent"

    check("a percent variable expands",
          _expand(r"%JARVIS_TEST_ROOT%\backtalk") == r"C:\somewhere\agent\backtalk")

    check("a plain absolute path is unchanged",
          _expand(r"D:\plain\path") == r"D:\plain\path")

    check("empty stays empty", _expand("") == "")

    home = os.path.expanduser("~")
    check("tilde still expands", _expand("~") == home)

    # The dangerous case: undefined variable.
    os.environ.pop("JARVIS_TEST_MISSING", None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        got = _expand(r"%JARVIS_TEST_MISSING%\thing")
    out = buf.getvalue()

    check("an undefined variable is returned verbatim, not half-resolved",
          got == r"%JARVIS_TEST_MISSING%\thing")
    check("an undefined variable is reported out loud",
          "JARVIS_TEST_MISSING" in out and "[config]" in out)
    check("the report names it as an environment variable",
          "environment variable" in out.lower())

    # Reporting is once per distinct variable, not once per call --- these
    # run on every stream open and every config load.
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        _expand(r"%JARVIS_TEST_MISSING%\again")
    check("the same missing variable is not reported twice",
          buf2.getvalue() == "")

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
