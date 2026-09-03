#!/usr/bin/env python3
"""claude-macropad daemon.

Bridges three things:
  * Claude Code hooks and statusline  -> unix socket   (hooks/forward.py)
  * the MacroPad firmware             <-> USB serial   (firmware/code.py)
  * tmux                              <- send-keys / select-window / new-window

Key layout (MacroPad numbering, 0 is top-left):

     0   1   2     agent keys: one Claude session each, color = state
     3   4   5     agent keys 4-5 | key 5 = usage gauge (press for details)
     6   7   8     /model sonnet | /model opus | /model fable
     9  10  11     plan mode     | manual mode | auto mode

  agent key = select that session AND bring its tmux window forward.
  model/mode keys act on the selected session; the one matching its current
  model / mode is lit blue. They are ignored while the session is blocked on
  a permission prompt, because typed text and Shift+Tab mean other things there.
  plan uses /plan; manual and auto cycle Shift+Tab from the mode the last hook
  reported (default -> acceptEdits -> plan -> auto -> default).

Encoder:
  sessions view   turn = select session and focus its tmux pane (debounced)
                  click = approve (Enter) it if it is blocked on a prompt
  launcher view   turn = choose a project      click = start claude there in tmux
  hold (0.6 s)    toggle between the two views
  The launcher is shown automatically while no Claude session is registered.

Colors are chosen for deuteranopia: only blue, yellow, red and white are used, and
red and green never appear together. Blue = Claude's move (working). Yellow = your
move (finished, waiting for a prompt). Red blinking = blocked on you (permission
prompt). Dim white = idle, nothing new. On the model and mode rows, blue marks the
selected session's current model and mode; the usage key runs white -> yellow -> red
as the five-hour window fills. The selected session is full brightness, others dimmed.

Run with --no-serial to drive the state machine without hardware; the LED grid
is printed whenever it changes. scripts/simulate.py generates fake events.
"""

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # only needed with real hardware
    serial = None

log = logging.getLogger("macropad")

HOME = os.path.expanduser("~")
SOCK_PATH = os.environ.get("CLAUDE_MACROPAD_SOCK", os.path.join(HOME, ".claude-macropad.sock"))
# One directory per line. Falls back to the project dirs Claude Code has recorded in ~/.claude.json.
PROJECTS_FILE = os.environ.get("CLAUDE_MACROPAD_PROJECTS", os.path.join(HOME, ".claude-macropad-projects"))
# Terminal used to attach when nothing is attached to tmux yet: "Ghostty", "iTerm" or "Terminal".
# Auto-detected from /Applications when unset.
TERMINAL_APP = os.environ.get("CLAUDE_MACROPAD_TERMINAL_APP")
SHELL = os.environ.get("SHELL", "/bin/zsh")
TMUX_SESSION = "claude"       # tmux session the launcher creates panes/windows in
# True: each new agent is a pane split to the right of the existing agents in the same window,
# widths evened out. False: each new agent gets its own tmux window.
LAUNCH_SPLIT = True

AGENT_KEYS = [0, 1, 2, 3, 4]
USAGE_KEY = 5
MODEL_KEYS = {6: "sonnet", 7: "opus", 8: "fable"}
MODE_KEYS = {9: "plan", 10: "default", 11: "auto"}
MODE_CYCLE = ["default", "acceptEdits", "plan", "auto"]   # Shift+Tab order; bypass not enabled here
USAGE_PAGE_S = 5.0        # how long the usage page stays up after pressing key 5
# Only sessions running inside tmux get a key. Anything else (desktop app, bare terminal)
# can't be commanded, would hijack the selection on a permission prompt, and hides the launcher.
TMUX_SESSIONS_ONLY = True
# Set to False to require an explicit agent-key press before command keys act.
AUTO_SELECT_ON_NEEDS_INPUT = True
LONG_PRESS_S = 0.6
ENCODER_FOCUS_DELAY_S = 0.15  # focus the tmux pane only once the encoder has stopped turning
NOTICE_S = 6.0                # how long a transient line ("starting web-apps") stays up
SHIFT_TAB_GAP_S = 0.15        # between successive Shift+Tab presses when cycling modes
RENDER_INTERVAL_S = 1.0       # also serves as the heartbeat the firmware watches
STALE_SWEEP_S = 10.0          # how often to drop sessions whose tmux pane is gone
ADAFRUIT_VID = 0x239A


class Lit(str):
    """A token sent to tmux as literal text (send-keys -l) rather than a key name."""


# Deuteranopia-safe palette. If yellow reads too close to red on the real LEDs, push more
# green into YELLOW (up to 255,255,0); the green channel is what separates them for an M-cone-weak eye.
WHITE = (70, 70, 70)
BLUE = (0, 70, 255)
YELLOW = (255, 230, 0)
RED = (255, 0, 0)
STATE_COLORS = {
    "idle": WHITE,
    "working": BLUE,
    "done": YELLOW,
    "needs_input": RED,      # blinks
    "error": WHITE,          # blinks; nothing feeds it yet
}
BLINK_STATES = ("needs_input", "error")
BLINK_HZ = 1.0
BLINK_OFF_LEVEL = 0.15   # off-phase brightness, so the key still marks where the session is
EMPTY_SLOT = (0, 0, 0)
DIM = 0.3
# Usage gauge (key 5) thresholds on the five-hour window.
USAGE_YELLOW_PCT = 50
USAGE_RED_PCT = 80
STATE_LABEL = {
    "idle": "idle",
    "working": "working",
    "needs_input": "needs input",
    "done": "done",
    "error": "error",
}


@dataclass
class Session:
    sid: str
    slot: int
    pane: str = None
    entrypoint: str = ""
    cwd: str = ""
    name: str = ""
    title: str = ""     # first prompt; distinguishes sessions started in the same directory
    state: str = "idle"
    message: str = ""
    last_tool: str = ""
    model: str = ""
    mode: str = ""      # permission_mode from the last hook payload
    ctx_pct: float = None
    cost: float = None
    updated: float = field(default_factory=time.monotonic)

    def touch(self):
        self.updated = time.monotonic()


# --------------------------------------------------------------------------- tmux

def tmux(*args):
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("tmux %s failed: %s", args[0], e)
        return None


def tmux_ok(r):
    return r is not None and r.returncode == 0


def tmux_send(pane, tokens):
    for tok in tokens:
        if isinstance(tok, Lit):
            tmux("send-keys", "-t", pane, "-l", str(tok))
        elif tok == "BTab":
            tmux("send-keys", "-t", pane, tok)
            time.sleep(SHIFT_TAB_GAP_S)
        else:
            tmux("send-keys", "-t", pane, tok)


def tmux_clients():
    r = tmux("list-clients", "-F", "#{client_name}")
    return r.stdout.split() if tmux_ok(r) else []


def tmux_focus(pane):
    r = tmux("display-message", "-p", "-t", pane, "#{session_name}")
    if not tmux_ok(r):
        return False
    session_name = r.stdout.strip()
    for client in tmux_clients():
        tmux("switch-client", "-c", client, "-t", session_name)
    tmux("select-window", "-t", pane)
    tmux("select-pane", "-t", pane)
    activate_terminal()
    return True


def tmux_live_panes():
    r = tmux("list-panes", "-a", "-F", "#{pane_id}")
    return set(r.stdout.split()) if tmux_ok(r) else None


def tmux_window_of(pane):
    """'session:index' of the window holding pane, or None."""
    r = tmux("display-message", "-p", "-t", pane, "#{session_name}:#{window_index}")
    return r.stdout.strip() if tmux_ok(r) and r.stdout.strip() else None


def tmux_rightmost_pane(window):
    r = tmux("list-panes", "-t", window, "-F", "#{pane_id} #{pane_left}")
    if not tmux_ok(r) or not r.stdout.strip():
        return None
    return max((l.split() for l in r.stdout.split("\n") if l.strip()), key=lambda x: int(x[1]))[0]


def tmux_launch(cwd, near=None, command="claude"):
    """Start `claude` in tmux in cwd. Returns the new pane id or None.

    With LAUNCH_SPLIT the new agent becomes a pane to the right of the existing agents:
    in the window holding `near` (the selected session's pane) when that is inside the
    claude session, otherwise the claude session's current window. Without it, or when
    the claude session doesn't exist yet, it gets a window of its own.
    """
    name = os.path.basename(cwd.rstrip("/"))
    # Login shell so claude is found on the user's PATH even when tmux was started by launchd.
    # The trailing exec keeps the pane (and Claude's last output) around after claude exits.
    cmd = "%s -lc '%s; exec %s -l'" % (SHELL, command, SHELL)
    if not tmux_ok(tmux("has-session", "-t", "=" + TMUX_SESSION)):
        r = tmux("new-session", "-d", "-s", TMUX_SESSION, "-c", cwd, "-n", name, "-P", "-F", "#{pane_id}", cmd)
    elif LAUNCH_SPLIT:
        window = tmux_window_of(near) if near else None
        if not window or not window.startswith(TMUX_SESSION + ":"):
            window = TMUX_SESSION + ":"
        target = tmux_rightmost_pane(window) or window
        r = tmux("split-window", "-h", "-t", target, "-c", cwd, "-P", "-F", "#{pane_id}", cmd)
        if tmux_ok(r):
            tmux("select-layout", "-t", window, "even-horizontal")
    else:
        r = tmux("new-window", "-t", TMUX_SESSION + ":", "-c", cwd, "-n", name, "-P", "-F", "#{pane_id}", cmd)
    if not tmux_ok(r):
        log.error("launch in %s failed: %s", cwd, (r.stderr if r else "tmux unavailable").strip())
        return None
    pane = r.stdout.strip()
    log.info("launched claude in %s (%s)", cwd, pane)
    if tmux_clients():
        tmux_focus(pane)
    else:
        open_terminal_attached()
    return pane


# --------------------------------------------------------------------------- terminal app

def terminal_app():
    if TERMINAL_APP:
        return TERMINAL_APP
    for app in ("Ghostty", "iTerm"):
        if os.path.isdir("/Applications/%s.app" % app):
            return app
    return "Terminal"


def _osascript(script):
    subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def activate_terminal():
    if TERMINAL_APP or os.environ.get("CLAUDE_MACROPAD_ACTIVATE"):
        _osascript('tell application "%s" to activate' % terminal_app())


def open_terminal_attached():
    """Open a terminal window attached to the tmux session, when no client is attached yet."""
    app = terminal_app()
    attach = "tmux attach -t %s" % TMUX_SESSION
    log.info("no tmux client attached; opening %s", app)
    if app == "Ghostty":
        subprocess.Popen(["open", "-na", "Ghostty.app", "--args", "--command=%s" % attach],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif app == "iTerm":
        _osascript('tell application "iTerm"\nactivate\ncreate window with default profile command "%s"\nend tell' % attach)
    else:
        _osascript('tell application "Terminal"\nactivate\ndo script "%s"\nend tell' % attach)


# --------------------------------------------------------------------------- projects

def load_projects():
    dirs = []
    if os.path.exists(PROJECTS_FILE):
        with open(PROJECTS_FILE) as f:
            dirs = [os.path.expanduser(l.strip()) for l in f if l.strip() and not l.startswith("#")]
    else:
        try:
            with open(os.path.join(HOME, ".claude.json")) as f:
                dirs = list((json.load(f).get("projects") or {}).keys())
        except (OSError, ValueError):
            dirs = []
    return [d for d in dirs if os.path.isdir(d)]


# --------------------------------------------------------------------------- board state

def short_name(cwd):
    base = os.path.basename(cwd.rstrip("/")) or cwd
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=1)
        branch = r.stdout.strip()
        if r.returncode == 0 and branch and branch != "HEAD":
            return "%s:%s" % (base, branch)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return base


def describe_tool(p):
    name = p.get("tool_name") or ""
    inp = p.get("tool_input") or {}
    detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("url") or ""
    if inp.get("file_path"):
        detail = os.path.basename(detail)
    detail = " ".join(str(detail).split())
    return ("%s: %s" % (name, detail)).strip(": ") if detail else name


def wrap(text, cols, lines):
    words, out, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + (1 if cur else 0) <= cols:
            cur = (cur + " " + w) if cur else w
        else:
            if cur:
                out.append(cur)
            cur = w[:cols]
        if len(out) == lines:
            break
    if cur and len(out) < lines:
        out.append(cur)
    return out[:lines]


class Board:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}          # sid -> Session
        self.selected = None        # sid
        self.dirty = True
        self.rows, self.cols = 5, 21  # replaced by the firmware's handshake
        self.launcher = False        # user-toggled launcher view
        self.projects = []
        self.project_idx = 0
        self.notice = ("", 0.0)      # transient top line, (text, expires_at)
        self.enc_down_at = None
        self.usage = {}              # rate_limits from the most recent statusline, any session
        self.usage_at = 0.0
        self.usage_page_until = 0.0
        self._focus_timer = None

    # -- registry

    def _free_slot(self):
        used = {s.slot for s in self.sessions.values()}
        for slot in AGENT_KEYS:
            if slot not in used:
                return slot
        return None

    def _ensure(self, sid, p):
        s = self.sessions.get(sid)
        if s is None:
            slot = self._free_slot()
            if slot is None:
                # Evict the least recently updated finished/idle session, if any.
                victims = sorted((x for x in self.sessions.values() if x.state in ("done", "idle")),
                                 key=lambda x: x.updated)
                if not victims:
                    log.info("no free agent key for session %s; ignoring", sid[:8])
                    return None
                slot = victims[0].slot
                self._remove(victims[0].sid)
            cwd = p.get("cwd") or (p.get("workspace") or {}).get("current_dir") or ""
            s = Session(sid=sid, slot=slot, cwd=cwd, name=short_name(cwd) if cwd else sid[:8])
            self.sessions[sid] = s
            if self.selected is None:
                self.selected = sid
            self.notice = ("", 0.0)
            log.info("session %s -> key %d (%s)", sid[:8], slot, s.name)
        return s

    def _remove(self, sid):
        s = self.sessions.pop(sid, None)
        if s:
            log.info("session %s left key %d", sid[:8], s.slot)
        if self.selected == sid:
            self.selected = next(iter(self._ordered()), None)

    def _ordered(self):
        return [s.sid for s in sorted(self.sessions.values(), key=lambda s: s.slot)]

    def slot_to_sid(self, slot):
        for s in self.sessions.values():
            if s.slot == slot:
                return s.sid
        return None

    def in_launcher(self):
        return self.launcher or not self.sessions

    # -- events from Claude Code

    def handle_event(self, p):
        name = p.get("hook_event_name")
        sid = p.get("session_id")
        if not sid or not name:
            return
        with self.lock:
            if name == "SessionEnd" and sid not in self.sessions:
                return  # short-lived helper invocations: don't flash a key for their exit
            if TMUX_SESSIONS_ONLY and sid not in self.sessions and not p.get("tmux_pane"):
                log.debug("%s %s ignored: not in tmux (%s)", sid[:8], name, p.get("entrypoint") or "cli")
                return
            s = self._ensure(sid, p)
            if s is None:
                return
            if p.get("tmux_pane"):
                s.pane = p["tmux_pane"]
            if p.get("entrypoint"):
                s.entrypoint = p["entrypoint"]
            if p.get("permission_mode"):
                s.mode = p["permission_mode"]
            s.touch()
            self.dirty = True

            if name == "SessionStart":
                s.state = "idle"
                s.message = ""
            elif name == "UserPromptSubmit":
                s.state = "working"
                s.message = ""
                s.last_tool = ""
                if not s.title:
                    s.title = " ".join((p.get("prompt") or "").split())
            elif name == "PreToolUse":
                s.state = "working"
                s.last_tool = describe_tool(p)
            elif name == "Notification":
                ntype = (p.get("notification_type") or "").lower()
                msg = p.get("message") or ""
                lower = msg.lower()
                if ntype == "permission_prompt" or "permission" in lower:
                    s.state = "needs_input"
                    s.message = s.last_tool or msg
                elif ntype == "idle_prompt" or "waiting" in lower:
                    s.state = "needs_input"
                    s.message = "waiting for you"
                else:
                    s.message = msg
                if s.state == "needs_input" and AUTO_SELECT_ON_NEEDS_INPUT:
                    self.selected = sid
            elif name == "Stop":
                s.state = "done"
                s.message = " ".join((p.get("last_assistant_message") or "").split())
            elif name == "SessionEnd":
                self._remove(sid)
            elif name == "StatusLine":
                s.model = (p.get("model") or {}).get("display_name") or s.model
                s.ctx_pct = (p.get("context_window") or {}).get("used_percentage", s.ctx_pct)
                s.cost = (p.get("cost") or {}).get("total_cost_usd", s.cost)
                if p.get("rate_limits"):
                    if not self.usage:
                        log.info("usage data present: %s", ", ".join(sorted(p["rate_limits"])))
                    self.usage = p["rate_limits"]
                    self.usage_at = time.monotonic()
            log.debug("%s %s -> %s", sid[:8], name, s.state)

    def sweep(self):
        live = tmux_live_panes()
        if live is None:
            return
        with self.lock:
            for s in list(self.sessions.values()):
                if s.pane and s.pane not in live:
                    log.info("pane %s for %s is gone", s.pane, s.sid[:8])
                    self._remove(s.sid)
                    self.dirty = True

    # -- input from the pad

    def on_key(self, n, pressed):
        log.debug("pad key %d %s", n, "down" if pressed else "up")
        if not pressed:
            return
        with self.lock:
            if n in AGENT_KEYS:
                sid = self.slot_to_sid(n)
                if not sid:
                    return
                self.selected = sid
                self.launcher = False
                self.dirty = True
                pane = self.sessions[sid].pane
            elif n == USAGE_KEY:
                self.usage_page_until = time.monotonic() + USAGE_PAGE_S
                self.dirty = True
                return
            elif n in MODEL_KEYS or n in MODE_KEYS:
                s = self.sessions.get(self.selected)
                if not s or not s.pane:
                    log.info("key %d: no selected session with a tmux pane", n)
                    return
                if s.state == "needs_input":
                    # Typed text and Shift+Tab both mean something else on a permission prompt.
                    self.notice = ("answer the prompt first", time.monotonic() + 3)
                    self.dirty = True
                    return
                tokens = self._tokens_for(n, s)
                if tokens is None:
                    return
                log.info("key %d -> %s (%s): %s", n, s.name, s.pane, " ".join(map(str, tokens)))
                threading.Thread(target=tmux_send, args=(s.pane, tokens), daemon=True).start()
                return
            else:
                return
        if pane:
            threading.Thread(target=tmux_focus, args=(pane,), daemon=True).start()

    def _tokens_for(self, n, s):
        if n in MODEL_KEYS:
            return [Lit("/model " + MODEL_KEYS[n]), "Enter"]
        target = MODE_KEYS[n]
        if target == "plan":
            return [Lit("/plan"), "Enter"]
        if s.mode not in MODE_CYCLE:
            self.notice = ("mode unknown yet", time.monotonic() + 3)
            self.dirty = True
            log.info("mode key: current mode %r unknown for %s", s.mode, s.name)
            return None
        presses = (MODE_CYCLE.index(target) - MODE_CYCLE.index(s.mode)) % len(MODE_CYCLE)
        if presses == 0:
            return []
        s.mode = target  # optimistic; the next hook payload corrects it if we were wrong
        return ["BTab"] * presses

    def on_encoder(self, delta):
        step = 1 if delta > 0 else -1
        with self.lock:
            self.dirty = True
            if self.in_launcher():
                if not self.projects:
                    self.projects = load_projects()
                if self.projects:
                    self.project_idx = (self.project_idx + step) % len(self.projects)
                return
            order = self._ordered()
            i = order.index(self.selected) if self.selected in order else 0
            self.selected = order[(i + step) % len(order)]
            # Follow the selection in tmux, but only after the knob settles so a quick spin
            # doesn't flip the terminal through every intermediate pane.
            if self._focus_timer:
                self._focus_timer.cancel()
            self._focus_timer = threading.Timer(ENCODER_FOCUS_DELAY_S, self._focus_selected)
            self._focus_timer.daemon = True
            self._focus_timer.start()

    def _focus_selected(self):
        with self.lock:
            s = self.sessions.get(self.selected)
            pane = s.pane if s else None
        if pane:
            tmux_focus(pane)

    def on_encoder_switch(self, pressed):
        if pressed:
            self.enc_down_at = time.monotonic()
            return
        held = time.monotonic() - (self.enc_down_at or time.monotonic())
        self.enc_down_at = None
        if held >= LONG_PRESS_S:
            with self.lock:
                if self.sessions:
                    self.launcher = not self.launcher
                    if self.launcher:
                        self.projects = load_projects()
                    self.dirty = True
            return
        with self.lock:
            if self.in_launcher():
                if not self.projects:
                    self.projects = load_projects()
                if not self.projects:
                    return
                cwd = self.projects[self.project_idx % len(self.projects)]
                sel = self.sessions.get(self.selected)
                near = sel.pane if sel else None
                self.launcher = False
                self.notice = ("starting " + os.path.basename(cwd), time.monotonic() + NOTICE_S)
                self.dirty = True
                threading.Thread(target=tmux_launch, args=(cwd, near), daemon=True).start()
                return
            s = self.sessions.get(self.selected)
            pane = s.pane if s else None
            approve = bool(s and s.state == "needs_input")
            if approve:
                log.info("approve -> %s (%s)", s.name, pane)
        if pane:
            def go():
                tmux_focus(pane)
                if approve:
                    tmux_send(pane, ["Enter"])
            threading.Thread(target=go, daemon=True).start()

    # -- output

    def _usage_lines(self):
        fh = (self.usage.get("five_hour") or {}).get("used_percentage")
        sd = (self.usage.get("seven_day") or {}).get("used_percentage")
        if fh is None and sd is None:
            return ["usage unavailable"]
        out = []
        for label, win in (("5h", "five_hour"), ("7d", "seven_day")):
            w = self.usage.get(win) or {}
            if w.get("used_percentage") is None:
                continue
            reset = ""
            if w.get("resets_at"):
                secs = max(0, int(w["resets_at"] - time.time()))
                reset = "%dh%02d" % divmod(secs // 60, 60) if secs < 86400 else "%dd" % (secs // 86400)
            out.append(("%s %3d%%  resets %s" % (label, round(w["used_percentage"]), reset)).rstrip())
        return out

    def _usage_color(self):
        fh = (self.usage.get("five_hour") or {}).get("used_percentage")
        if fh is None:
            return EMPTY_SLOT
        base = RED if fh >= USAGE_RED_PCT else YELLOW if fh >= USAGE_YELLOW_PCT else WHITE
        level = 0.25 + 0.75 * min(fh, 100) / 100.0
        return tuple(int(v * level) for v in base)

    def _render_launcher(self):
        if not self.projects:
            self.projects = load_projects()
        cols, rows = self.cols, self.rows
        lines = ["new claude in:"]
        if not self.projects:
            return lines + ["no projects", "~/.claude-macropad-", "projects"][: rows - 1]
        n = len(self.projects)
        visible = rows - 1
        start = max(0, min(self.project_idx - visible // 2, n - visible))
        for i in range(start, min(n, start + visible)):
            name = os.path.basename(self.projects[i].rstrip("/"))
            mark = ">" if i == self.project_idx % n else " "
            lines.append((mark + name)[:cols])
        usage = self._usage_lines()
        if len(lines) + 1 + len(usage) <= rows and self.usage:
            lines += [""] + usage
        return lines

    def _render_session(self, sel):
        cols, rows = self.cols, self.rows
        head = ("%d %s" % (sel.slot + 1, sel.title or sel.name))[:cols]
        stats = []
        if sel.ctx_pct is not None:
            stats.append("%d%%" % round(sel.ctx_pct))
        if sel.cost is not None:
            stats.append("$%.2f" % sel.cost)
        if sel.model:
            stats.append(sel.model)
        foot = " ".join(stats)[:cols]
        body_rows = rows - 1 - (1 if foot else 0)
        body = wrap(sel.message or sel.last_tool or STATE_LABEL.get(sel.state, ""), cols, body_rows)
        lines = [head] + body
        if foot:
            lines += [""] * (rows - 1 - len(lines)) + [foot]
        return lines

    def blinking(self):
        with self.lock:
            return any(s.state in BLINK_STATES for s in self.sessions.values())

    def render(self):
        blink_on = int(time.monotonic() * BLINK_HZ * 2) % 2 == 0
        with self.lock:
            self.dirty = False
            colors = [EMPTY_SLOT] * 12
            for s in self.sessions.values():
                c = STATE_COLORS.get(s.state, EMPTY_SLOT)
                if s.state in BLINK_STATES and not blink_on:
                    c = tuple(int(v * BLINK_OFF_LEVEL) for v in c)
                elif s.sid != self.selected:
                    c = tuple(int(v * DIM) for v in c)
                colors[s.slot] = c
            sel = self.sessions.get(self.selected)
            colors[USAGE_KEY] = self._usage_color()
            cur_model = (sel.model or "").lower() if sel else ""
            for n, alias in MODEL_KEYS.items():
                lit = sel is not None and alias in cur_model
                colors[n] = BLUE if lit else tuple(int(v * DIM) for v in WHITE)
            for n, mode in MODE_KEYS.items():
                lit = sel is not None and sel.mode == mode
                colors[n] = BLUE if lit else tuple(int(v * DIM) for v in WHITE)

            if time.monotonic() < self.usage_page_until:
                lines = ["usage"] + self._usage_lines()
                self.dirty = True
            elif self.in_launcher():
                lines = self._render_launcher()
            else:
                lines = self._render_session(sel)
            text, until = self.notice
            if text and time.monotonic() < until:
                lines[0] = text[: self.cols]
                self.dirty = True   # keep re-rendering so the notice expires on time
            return colors, lines[: self.rows]


# --------------------------------------------------------------------------- pad link

class Pad:
    def __init__(self, board, port=None, mock=False):
        self.board = board
        self.port = port
        self.mock = mock
        self.ser = None
        self._last_printed = None
        self._last_colors = None
        self._last_lines = None

    def start(self):
        if self.mock:
            log.info("mock pad: no serial, printing LED grid on change")
            return
        if serial is None:
            log.error("pyserial not installed; run with --no-serial or pip install -r daemon/requirements.txt")
            sys.exit(2)
        threading.Thread(target=self._loop, daemon=True).start()

    def _candidates(self):
        if self.port:
            return [self.port]
        ports = [p.device for p in serial.tools.list_ports.comports() if p.vid == ADAFRUIT_VID]
        return sorted(ports)

    def _handshake(self, device):
        s = serial.Serial(device, 115200, timeout=0.2)
        s.reset_input_buffer()
        s.write(b'{"t":"hello"}\n')
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            line = s.readline()
            if line.startswith(b"{"):
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("t") == "hi":
                    rows, cols = msg.get("rows"), msg.get("cols")
                    if rows and cols:
                        self.board.rows, self.board.cols = int(rows), int(cols)
                    log.info("pad on %s (fw %s, %sx%s chars)", device, msg.get("fw"), cols, rows)
                    return s
        s.close()
        return None

    def _connect(self):
        for device in self._candidates():
            try:
                s = self._handshake(device)
            except (OSError, serial.SerialException) as e:
                log.debug("%s: %s", device, e)
                continue
            if s:
                return s
        return None

    def _loop(self):
        while True:
            self.ser = self._connect()
            if self.ser is None:
                time.sleep(2)
                continue
            self._last_colors = self._last_lines = None
            self.board.dirty = True
            try:
                while True:
                    line = self.ser.readline()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    self._dispatch(msg)
            except (OSError, serial.SerialException) as e:
                log.warning("pad disconnected: %s", e)
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None

    def _dispatch(self, msg):
        t = msg.get("t")
        if t == "key":
            self.board.on_key(int(msg.get("n", -1)), msg.get("a") == "p")
        elif t == "enc":
            log.debug("pad encoder %+d", int(msg.get("d", 0)))
            self.board.on_encoder(int(msg.get("d", 0)))
        elif t == "encsw":
            log.debug("pad encoder switch %s", "down" if msg.get("a") == "p" else "up")
            self.board.on_encoder_switch(msg.get("a") == "p")

    def send(self, obj):
        if self.mock:
            return
        ser = self.ser
        if ser is None:
            return
        try:
            ser.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        except (OSError, serial.SerialException) as e:
            log.warning("write failed: %s", e)

    def show(self, colors, lines):
        if self.mock:
            snapshot = (tuple(colors), tuple(lines))
            if snapshot != self._last_printed:
                self._last_printed = snapshot
                rows = ["  ".join("%3d,%3d,%3d" % c for c in colors[r * 3:(r + 1) * 3]) for r in range(4)]
                print("\n".join(rows + ["  | " + l for l in lines] + ["-" * 44]), flush=True)
            return
        # Send only what changed: every OLED write blanks the rows it touches for a frame,
        # so an unconditional repaint on the heartbeat is visible flicker.
        colors, lines = [list(c) for c in colors], list(lines)
        sent = False
        if colors != self._last_colors:
            self.send({"t": "leds", "c": colors})
            self._last_colors = colors
            sent = True
        if lines != self._last_lines:
            self.send({"t": "oled", "l": lines})
            self._last_lines = lines
            sent = True
        if not sent:
            self.send({"t": "ping"})  # keeps the firmware's offline timer fed


# --------------------------------------------------------------------------- socket server

def bind_socket(path):
    """Bind in the main thread so a bad path (too long, unwritable) is fatal, not a dead thread."""
    if os.path.exists(path):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
    except OSError as e:
        # macOS caps AF_UNIX paths at 104 bytes; the default under ~ is fine.
        log.error("cannot bind %s: %s", path, e)
        sys.exit(2)
    os.chmod(path, 0o600)
    srv.listen(16)
    log.info("listening on %s", path)
    return srv


def serve_socket(board, srv):
    # Handled inline, not per-thread: hook events for one session can arrive
    # milliseconds apart (PreToolUse then Notification) and must apply in order.
    # forward.py writes one line and closes, so each connection is sub-millisecond.
    while True:
        conn, _ = srv.accept()
        _handle_conn(board, conn)


def _handle_conn(board, conn):
    conn.settimeout(0.5)
    data = b""
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
    except OSError:
        pass
    finally:
        conn.close()
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            board.handle_event(json.loads(line))
        except ValueError:
            log.warning("bad payload: %r", line[:120])


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial device (default: probe Adafruit ports for the firmware handshake)")
    ap.add_argument("--no-serial", action="store_true", help="run without hardware, print the LED grid")
    ap.add_argument("--sock", default=SOCK_PATH, help="unix socket path (default %(default)s)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    board = Board()
    srv = bind_socket(args.sock)
    pad = Pad(board, port=args.port, mock=args.no_serial)
    pad.start()
    threading.Thread(target=serve_socket, args=(board, srv), daemon=True).start()

    last_sweep = 0.0
    last_render = 0.0
    try:
        while True:
            now = time.monotonic()
            if now - last_sweep > STALE_SWEEP_S:
                board.sweep()
                last_sweep = now
            interval = 0.5 / BLINK_HZ / 2 if board.blinking() else RENDER_INTERVAL_S
            if board.dirty or now - last_render > interval:
                colors, lines = board.render()
                pad.show(colors, lines)
                last_render = now
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        if os.path.exists(args.sock):
            os.unlink(args.sock)


if __name__ == "__main__":
    main()
