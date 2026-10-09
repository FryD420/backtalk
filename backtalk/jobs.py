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
"""Names for background jobs: one short label, one spoken form.

A finished task arrives with whatever the CLI had for it: the model's
description ("Build the APK", "Log state in daily note and back up"),
or, when the model gave none, the raw shell command, heredocs and all.
Read out as is, that is clunky ("the cd-npm-ci-21-tail-3 job"). So:

  job_label(info)    a short title for a screen, a log line or the
                     .voice_unspoken "what" field: "Build the APK",
                     "Beepies spike test run", "Something" when unknown.
  spoken_name(info)  the same job as the subject of a spoken sentence:
                     "the agent building the APK", "the Beepies spike
                     test run", "Something".

Pure functions, no imports from the rest of backtalk, so the reader,
the floor and any face can share them.
"""
import re

UNKNOWN = "Something"
_MAX_WORDS = 8

# Leading verbs of a model-written description, and how they read as
# "the agent <-ing ...>". A first word not listed here is left alone
# (it may be a noun: "Beepies build"), so the list only has to be right,
# not complete.
_GERUND = {
    "add": "adding", "analyze": "analyzing", "audit": "auditing",
    "back": "backing", "build": "building", "bump": "bumping",
    "check": "checking", "clean": "cleaning", "clone": "cloning",
    "commit": "committing", "compare": "comparing",
    "compile": "compiling", "copy": "copying", "create": "creating",
    "debug": "debugging", "deploy": "deploying",
    "diagnose": "diagnosing", "download": "downloading",
    "draft": "drafting", "dry-run": "dry-running", "edit": "editing",
    "explore": "exploring", "export": "exporting", "fetch": "fetching",
    "find": "finding", "fix": "fixing", "generate": "generating",
    "get": "getting", "install": "installing",
    "investigate": "investigating", "list": "listing", "load": "loading",
    "log": "logging", "make": "making", "merge": "merging",
    "migrate": "migrating", "move": "moving", "package": "packaging",
    "plan": "planning", "probe": "probing", "pull": "pulling",
    "push": "pushing", "read": "reading", "rebase": "rebasing",
    "rebuild": "rebuilding", "refactor": "refactoring",
    "render": "rendering", "research": "researching",
    "restart": "restarting", "review": "reviewing", "run": "running",
    "scan": "scanning", "search": "searching", "set": "setting",
    "ship": "shipping", "sleep": "sleeping", "start": "starting",
    "stop": "stopping", "summarize": "summarizing", "sync": "syncing",
    "test": "testing", "update": "updating", "upload": "uploading",
    "verify": "verifying", "wait": "waiting", "watch": "watching",
    "write": "writing",
}
# never the last word of a cut-down label
_DANGLING = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "into",
             "of", "on", "or", "per", "the", "then", "to", "with", "&"}
# path segments that say nothing about which project a command is in
_GENERIC_DIRS = {"", ".", "..", "~", "c", "d", "e", "f", "app", "bin", "build",
                 "client", "dev", "dist", "home", "lib", "mnt", "opt", "out",
                 "server", "src", "test", "tests", "tmp", "temp", "users",
                 "usr", "var", "scripts", "documents", "desktop"}
_SHELL_FIRST = {"bash", "cargo", "cat", "cd", "curl", "echo", "find", "for",
                "git", "go", "godot", "gradle", "grep", "ls", "make", "node",
                "npm", "npx", "pip", "pnpm", "powershell", "pwsh", "py",
                "pytest", "python", "python3", "sh", "sleep", "timeout",
                "uv", "while", "yarn"}
# what a shell command was doing, first match wins
_ACTIONS = (
    (re.compile(r"\b(py)?tests?\b|\btest_|\bjest\b|\bvitest\b"), "test run"),
    (re.compile(r"\bgit\b[^|;&]*\bpush\b"), "push"),
    (re.compile(r"\bgit\b[^|;&]*\b(fetch|pull|remote\s+update)\b"), "fetch"),
    (re.compile(r"\bgit\b[^|;&]*\bcommit\b"), "commit"),
    (re.compile(r"\bgit\b[^|;&]*\bclone\b"), "clone"),
    (re.compile(r"\b(build|assemble\w*|compile|bundle)\b|--export"), "build"),
    (re.compile(r"\b(install|npm\s+ci|uv\s+sync)\b"), "install"),
    (re.compile(r"^\s*sleep\b"), "timer"),
)


def _looks_like_command(desc: str) -> bool:
    if "\n" in desc or re.search(r"&&|\|\||[|;$<>=]", desc):
        return True
    first = desc.split()[0]
    return first.islower() and (first in _SHELL_FIRST or "/" in first
                                or "\\" in first)


def _project(cmd: str) -> str:
    """The project a command ran in, from the first path in it:
    /e/dev/beepies-spike-step2/server -> 'Beepies spike'."""
    for tok in cmd.split():
        if "/" not in tok and "\\" not in tok:
            continue
        segs = [s for s in re.split(r"[\\/]+", tok.strip("'\";&|()"))]
        for seg in reversed(segs):
            low = seg.lower().rstrip(":")
            if low in _GENERIC_DIRS or "." in seg or "$" in seg:
                continue
            words = [w for w in re.split(r"[-_\s]+", seg)
                     if w and not any(c.isdigit() for c in w)][:2]
            if words:
                return " ".join([words[0].capitalize()] + words[1:])
        break                       # only the first path says where
    return ""


def _from_command(cmd: str):
    """(label, spoken) for a raw shell command."""
    action = "command"
    for rx, name in _ACTIONS:
        if rx.search(cmd):
            action = name
            break
    proj = _project(cmd)
    if proj:
        label = f"{proj} {action}"
        return label, f"the {label}"
    return action.capitalize(), f"the {action}"


def _noun(info) -> str:
    typ = str(info.get("type") or "").lower()
    if "agent" in typ or "teammate" in typ:
        return "agent"
    if "bash" in typ or "shell" in typ:
        return "job"
    return "task"


def _from_description(desc: str, info):
    """(label, spoken) for a model-written description."""
    # the first clause: "Dry-run fetch all remotes, show exit codes" ->
    # "Dry-run fetch all remotes" (a drive's "E:" is not a clause end)
    first = re.split(r"[,;:](?:\s|$)|\s\(|\s[-–—]\s", desc, maxsplit=1)[0]
    words = [w for w in first.split() if "/" not in w and "\\" not in w]
    words = [re.sub(r"[^\w'+-]", "", w) for w in words]
    words = [w for w in words if w][:_MAX_WORDS]
    while words and words[-1].lower() in _DANGLING:
        words.pop()
    if not words:
        return None
    label = " ".join(words)
    label = label[0].upper() + label[1:]
    verb = _GERUND.get(words[0].lower())
    if not verb:
        return label, label
    # "... and back up" -> "... and backing up": keep the verbs parallel
    rest = [_GERUND.get(w.lower(), w) if words[i] in ("and", "then")
            else w for i, w in enumerate(words[1:])]
    return label, " ".join(["the", _noun(info), verb] + rest)


def _names(info):
    if not info:
        return UNKNOWN, UNKNOWN
    desc = " ".join(str(info.get("description") or "").split())
    desc = desc.replace("`", "").strip()
    if not desc:
        return UNKNOWN, UNKNOWN
    if _looks_like_command(desc):
        return _from_command(str(info.get("description")))
    return _from_description(desc, info) or (UNKNOWN, UNKNOWN)


def job_label(info) -> str:
    """Short title for the job: 'Build the APK', 'Beepies spike test
    run', 'Something'. For the log, .voice_unspoken and any face."""
    return _names(info)[0]


def spoken_name(info) -> str:
    """The job as the subject of a spoken line: 'the agent building the
    APK', 'the Beepies spike test run', 'Something'."""
    return _names(info)[1]
