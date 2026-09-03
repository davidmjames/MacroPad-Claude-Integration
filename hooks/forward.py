#!/usr/bin/env python3
"""Forward a Claude Code hook or statusline payload to the claude-macropad daemon.

Reads the JSON Claude Code puts on stdin, tags it with the tmux pane it ran in,
and writes it to the daemon's unix socket. Always exits 0 and never waits more
than a fraction of a second: a dead daemon must not slow Claude down.

  hook command:        python3 /path/to/forward.py
  statusline command:  python3 /path/to/forward.py --event StatusLine --print

--event overrides hook_event_name (statusline payloads don't carry one).
--print also prints a one-line status line so the terminal still shows one.
"""

import json
import os
import socket
import sys

SOCK = os.environ.get("CLAUDE_MACROPAD_SOCK", os.path.expanduser("~/.claude-macropad.sock"))


def format_statusline(p):
    model = (p.get("model") or {}).get("display_name") or "?"
    cwd = (p.get("workspace") or {}).get("current_dir") or p.get("cwd") or ""
    parts = ["[%s]" % model, os.path.basename(cwd)]
    pct = (p.get("context_window") or {}).get("used_percentage")
    if pct is not None:
        parts.append("ctx %d%%" % round(pct))
    cost = (p.get("cost") or {}).get("total_cost_usd")
    if cost is not None:
        parts.append("$%.2f" % cost)
    return "  ".join(x for x in parts if x)


def main():
    args = sys.argv[1:]
    event = args[args.index("--event") + 1] if "--event" in args else None
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if event:
        payload["hook_event_name"] = event
    payload["tmux_pane"] = os.environ.get("TMUX_PANE")
    # "claude-desktop" for the desktop app's Code tab; such sessions have no pane to aim at.
    payload["entrypoint"] = os.environ.get("CLAUDE_CODE_ENTRYPOINT")

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect(SOCK)
        s.sendall((json.dumps(payload) + "\n").encode())
        s.close()
    except OSError:
        pass

    if "--print" in args:
        print(format_statusline(payload))


if __name__ == "__main__":
    main()
