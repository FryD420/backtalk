# backtalk: talk to your Claude Code agent out loud.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed-text cleaners, extracted from main.py so the inbox can share
them. Pure functions, no I/O — run this file directly."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtalk.typed import clean_typed, join_paste

# 1. plain text survives untouched
assert clean_typed("hello there") == "hello there"

# 2. surrounding whitespace goes
assert clean_typed("   hello   ") == "hello"

# 3. terminal-copy gutter glyphs are stripped, repeatedly
assert clean_typed("> hello") == "hello"
assert clean_typed("| hello") == "| hello"          # ASCII pipe is NOT a gutter
assert clean_typed("│ hello") == "hello"
assert clean_typed("▎ hello") == "hello"
assert clean_typed("> > hello") == "hello"

# 4. an all-gutter line collapses to empty
assert clean_typed(">") == ""

# 5. a pasted blob becomes ONE message: gutters scrubbed, lines joined,
#    whitespace collapsed
assert join_paste("line one\nline two") == "line one line two"
assert join_paste("> a\n>  b\n\n> c") == "a b c"
assert join_paste("") == ""

print("test_typed_clean: OK")
