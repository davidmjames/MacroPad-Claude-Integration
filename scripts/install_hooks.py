#!/usr/bin/env python3
"""Merge hooks/settings.snippet.json into ~/.claude/settings.json.

The snippet's __REPO__ placeholder is rendered to this checkout's absolute path,
so the hooks keep working if the repo is moved: re-run this after a move.
Idempotent: existing hook entries that run forward.py are replaced (so a stale
path is fixed), other hooks are left alone. statusLine is set if absent or if it
already points at forward.py; use --force to replace an unrelated one.
A timestamped backup of settings.json is written first.

Hooks are snapshotted when a Claude Code session starts, so sessions already
running will not pick these up until restarted.
"""

import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
SNIPPET = os.path.join(REPO, "hooks", "settings.snippet.json")
SETTINGS = os.path.expanduser("~/.claude/settings.json")


def main():
    force = "--force" in sys.argv
    with open(SNIPPET) as f:
        snippet = json.loads(f.read().replace("__REPO__", REPO))
    settings = {}
    if os.path.exists(SETTINGS):
        with open(SETTINGS) as f:
            settings = json.load(f)
        backup = "%s.bak-%s" % (SETTINGS, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(SETTINGS, backup)
        print("backup:", backup)

    changed = []
    current = settings.get("statusLine")
    if force or current is None or "forward.py" in json.dumps(current):
        if current != snippet["statusLine"]:
            settings["statusLine"] = snippet["statusLine"]
            changed.append("statusLine")
    else:
        print("statusLine already set to something else; rerun with --force to replace it")

    hooks = settings.setdefault("hooks", {})
    for event, entries in snippet["hooks"].items():
        existing = hooks.setdefault(event, [])
        kept = [e for e in existing if "forward.py" not in json.dumps(e)]
        if kept + entries != existing:
            hooks[event] = kept + entries
            changed.append("hooks." + event)

    with open(SETTINGS, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print("updated:", ", ".join(changed) if changed else "nothing (already installed)")


if __name__ == "__main__":
    main()
