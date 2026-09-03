#!/usr/bin/env python3
"""Send a scripted sequence of fake Claude Code hook events to the daemon.

Lets you watch the LEDs and OLED (or the --no-serial grid) without running
real Claude sessions. Two sessions come up, both work, one hits a permission
prompt, both finish, one exits.

  python3 scripts/simulate.py            # ~10 s of events
  python3 scripts/simulate.py --fast     # no delays
  python3 scripts/simulate.py --pane %3  # tag session A with a real tmux pane id
"""

import json
import os
import socket
import sys
import time

SOCK = os.environ.get("CLAUDE_MACROPAD_SOCK", os.path.expanduser("~/.claude-macropad.sock"))
FAST = "--fast" in sys.argv
PANE = sys.argv[sys.argv.index("--pane") + 1] if "--pane" in sys.argv else None


def send(**payload):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    s.connect(SOCK)
    s.sendall((json.dumps(payload) + "\n").encode())
    s.close()
    print("->", payload["hook_event_name"], payload["session_id"][:8], payload.get("state", ""))


def pause(seconds):
    if not FAST:
        time.sleep(seconds)


A = dict(session_id="aaaaaaaa-1111", cwd=os.path.expanduser("~/repos/daysheets/travel-api"), tmux_pane=PANE)
B = dict(session_id="bbbbbbbb-2222", cwd=os.path.expanduser("~/repos/daysheets/web-apps"), tmux_pane=None)

send(hook_event_name="SessionStart", source="startup", **A)
send(hook_event_name="SessionStart", source="startup", **B)
pause(1)
send(hook_event_name="UserPromptSubmit", prompt="fix the flaky test", **A)
send(hook_event_name="UserPromptSubmit", prompt="add a column", **B)
pause(1)
send(hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": "pytest tests/test_flights.py -x"}, **A)
send(hook_event_name="StatusLine", model={"display_name": "Fable"},
     context_window={"used_percentage": 42.0}, cost={"total_cost_usd": 1.2345}, **A)
pause(1)
send(hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": "git push origin flights-main"}, **A)
send(hook_event_name="Notification", notification_type="permission_prompt",
     message="Claude needs your permission to use Bash", **A)
pause(3)
send(hook_event_name="Stop", last_assistant_message="Added the column and a migration.", **B)
pause(1)
send(hook_event_name="Stop", last_assistant_message="Pushed. The flaky test was a timezone fixture.", **A)
pause(2)
send(hook_event_name="SessionEnd", reason="exit", **B)
print("done")
