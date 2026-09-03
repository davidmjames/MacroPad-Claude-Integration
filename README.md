# MacroPad-Claude-Integration

Internally the pieces are still called `claude-macropad`: the launchd label, the unix socket, the log file.

Turns an [Adafruit MacroPad RP2040](https://www.adafruit.com/product/5128) into a
control surface for Claude Code sessions running in tmux, in the spirit of the
Work Louder Codex Micro: per-session status lights, one-click approve aimed at the
right session, model and permission-mode keys, and a usage gauge.

```
 ┌────┬────┬────┐
 │ S1 │ S2 │ S3 │   agent keys: one Claude session each
 ├────┼────┼────┤   white idle · blue working · yellow done · RED BLINKING needs input
 │ S4 │ S5 │ ▮  │   bright = selected session, dim = others · key 6 = usage gauge
 ├────┼────┼────┤
 │ So │ Op │ Fa │   /model sonnet · opus · fable        (current one lit blue)
 ├────┼────┼────┤
 │ Pl │ Ma │ Au │   plan · manual · auto permission mode (current one lit blue)
 └────┴────┴────┘
   (o)  encoder: turn = select session and focus its pane,
        click = approve it if it is blocked on a prompt
        hold 0.6 s = launcher: turn picks a project, click starts claude there
```

* **Colors** are picked for deuteranopia: blue, yellow, red and white only, and red and green never appear together. Blue is Claude's move, yellow is your move (finished, waiting for a prompt), red blinking is blocked on you (a permission prompt), dim white is idle. On the model and mode rows, blue marks the selected session's current model and mode; the usage key runs white, yellow, red as the five-hour window fills. If yellow reads too close to red on your LEDs, raise the green channel in `YELLOW` at the top of the daemon.
* **Agent key**: select that session and switch tmux to its window.
* **Encoder turn**: select the previous or next session; tmux follows once the knob settles, so a quick spin doesn't flip through every pane. **Encoder click**: if the selected session is blinking red, press Enter to accept its permission prompt. Otherwise it just re-focuses the pane.
* **Model keys** type `/model sonnet|opus|fable` into the selected session. Note `/model` also saves that choice as the default for new sessions. The key matching the session's current model is lit blue.
* **Mode keys**: plan sends `/plan`; manual and auto cycle Shift+Tab from the mode the last hook reported, in Claude Code's order (manual, accept edits, plan, auto). The daemon can't see a Shift+Tab you press by hand between hook events, so if a mode key lands one step off, press it again. The key matching the current mode is lit blue.
* Model and mode keys are ignored while the selected session is blocked on a permission prompt, since typed text and Shift+Tab mean other things there; the OLED says "answer the prompt first".
* **Usage key** (6): the LED is a gauge of the five-hour rate-limit window, white under 50%, yellow to 80%, red above, brightness tracking the percentage. Press it for five-hour and seven-day percentages with reset times on the OLED for a few seconds. The launcher view also shows them in its spare rows. Rate-limit data comes from the statusline JSON; the LED stays off if the plan doesn't expose it.
* The OLED is 21 columns by 5 rows, so it shows only what the LEDs can't: top row is the selected session's key number and `repo:branch`, the middle rows are the command awaiting approval (or Claude's last line, or the state when there's nothing better), the bottom row is `42% $1.23 Fable`.

## Running Claude in tmux

The daemon aims keystrokes with `tmux send-keys`, so each Claude session needs to live in a tmux pane. Any layout works; the launcher's own convention is one tmux session named `claude` with the agents side by side as panes. By hand:

```bash
tmux new-session -A -s claude -c ~/repos/daysheets/travel-api
claude
```

For a second session, split (`Ctrl-b %`) or open a window (`Ctrl-b c`) and run `claude` there. Panes and windows in other tmux sessions work too; pane ids are unique server-wide and the daemon switches the client to the right tmux session when you press an agent key. Agent keys fill in the order sessions start.

### Launching from the pad

While no Claude session is registered, the OLED shows the launcher: a list of project directories. Turn the encoder to pick one and click to run `claude` there. The first agent gets a tmux session named `claude`; each further agent is split in as a new pane to the right of the existing agents, in the window holding the selected session, with the widths evened out. Set `LAUNCH_SPLIT = False` in the daemon to get one tmux window per agent instead. If no terminal is attached to tmux yet, the daemon opens one, Ghostty or iTerm if installed, otherwise Terminal, already attached. Hold the encoder for 0.6 s to open the launcher while sessions exist, and again to go back.

The project list comes from `~/.claude-macropad-projects`, one directory per line, `~` allowed. Without that file it falls back to the directories Claude Code has recorded in `~/.claude.json`, which is every project you've opened Claude in.

If your work spans several repos under one workspace root, list only the root (for example `~/repos/daysheets`) so every session can reach all of them. Sessions started in the same directory would all be named alike, so the OLED's top row switches from the directory name to the session's first prompt as soon as you send one.

## Layout of this repo

| Path | What |
|---|---|
| `firmware/code.py`, `firmware/boot.py` | CircuitPython firmware. Dumb by design: reports keys, paints what it's told. |
| `daemon/macropad_daemon.py` | Host daemon. Unix socket in from hooks, USB serial to the pad, tmux out. |
| `hooks/forward.py` | The hook and statusline command. Forwards Claude Code's JSON to the socket. |
| `hooks/settings.snippet.json` | Hook + statusline config to merge into `~/.claude/settings.json`. |
| `scripts/install_hooks.py` | Merges the snippet with a backup. |
| `scripts/simulate.py` | Fake hook events for testing without Claude or the pad. |
| `launchd/`, `install.sh` | Runs the daemon as a launchd user agent. |

## Setup

### 1. Firmware

1. Put CircuitPython 9.x on the pad: [download the MacroPad UF2](https://circuitpython.org/board/adafruit_macropad_rp2040/), hold the encoder while plugging in USB (or double-tap reset), drag the UF2 onto `RPI-RP2`.
2. Install the library bundle onto the `CIRCUITPY` drive:
   ```bash
   pip install circup && circup install adafruit_macropad
   ```
3. Copy `firmware/boot.py` and `firmware/code.py` to the root of `CIRCUITPY`.
4. Unplug and replug. `boot.py` only runs on a hard reset, and it enables the second serial port the daemon uses. The OLED should read "daemon offline".

### 2. Daemon

```bash
./install.sh
```

That creates `.venv`, installs pyserial, and loads a launchd agent that restarts on failure. Log at `~/Library/Logs/claude-macropad.log`. After editing the daemon, restart it with:

```bash
launchctl kickstart -k gui/$(id -u)/com.claude-macropad.daemon
```

Don't `kill` it or run a second copy by hand while the agent is loaded: launchd respawns it within a second and the two instances fight over the serial port. Re-run `./install.sh` after changing the plist template. To run it in the foreground instead, unload the agent first (`launchctl bootout gui/$(id -u)/com.claude-macropad.daemon`), then:

```bash
./.venv/bin/python daemon/macropad_daemon.py --verbose
```

The daemon finds the pad by probing Adafruit USB serial ports for the firmware handshake, so the two CDC ports the pad exposes don't need telling apart. Pass `--port /dev/cu.usbmodemXXXX` to skip probing.

### 3. Hooks and statusline

```bash
python3 scripts/install_hooks.py
```

Backs up `~/.claude/settings.json`, then adds SessionStart, UserPromptSubmit, PreToolUse, Notification, Stop and SessionEnd hooks plus a statusline, all pointing at `hooks/forward.py`. Existing hooks are left alone; an existing statusline is left alone unless you pass `--force`. Claude Code snapshots hooks when a session starts, so restart running sessions.

The statusline still prints a normal `[model] dir ctx 42% $1.23` line in your terminal while forwarding the same JSON to the pad.

## Testing without hardware

```bash
./.venv/bin/python daemon/macropad_daemon.py --no-serial &
python3 scripts/simulate.py
```

The daemon prints the 12-key color grid and OLED lines whenever they change. Pass `--pane %N` to `simulate.py` (get the id from `tmux display -p '#{pane_id}'`) and press agent key 1 or the command keys on the real pad to see tmux react.

## How it hangs together

* Every hook payload carries `session_id`; `forward.py` adds the `TMUX_PANE` it ran in. That pair is all the daemon needs to own a key and aim `tmux send-keys` at the right pane.
* UserPromptSubmit → working. PreToolUse → remembers the tool and command so the OLED can show what needs approving. Notification with `permission_prompt` → needs input (and auto-selects that session; set `AUTO_SELECT_ON_NEEDS_INPUT = False` to require a key press). Stop → done. SessionEnd, or the tmux pane disappearing, frees the key.
* Sessions outside tmux (the desktop app, a bare terminal) are ignored: the hooks still fire, but the daemon drops events that carry no tmux pane, because such a session could never be commanded and would only steal the selection or hide the launcher. Set `TMUX_SESSIONS_ONLY = False` to light keys for them anyway. Supporting the desktop app properly means a PreToolUse hook that blocks on a pad press and returns the permission decision itself; that would be a deliberate second mode.

## Gotchas

* `boot.py` changes need a hard reset (unplug), not Ctrl-D.
* Option+O for fast mode reaches Claude Code as `ESC o` via tmux, which is what Claude Code expects; no terminal Option-as-Meta setting is involved.
* Push-to-talk is available but off: set `PTT_KEY = 11` in the firmware to make key 12 hold Space (Claude Code voice dictation) as a real HID keystroke. Set `CLAUDE_MACROPAD_TERMINAL_APP` (in the plist) to your terminal's app name and selecting a session will also bring that app frontmost.
* The launcher runs `claude` through a login shell, so it is found on your PATH even though launchd started tmux. The window stays open after Claude exits so you can read its last output.
* If your hooks aren't installed yet, a launched session won't register, and the launcher will still be showing. Run `scripts/install_hooks.py` first.
* No Bluetooth: the RP2040 has none. No joystick: the pad has none. The Codex Micro has both; the MacroPad has a screen instead.
