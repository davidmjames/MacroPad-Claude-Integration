"""claude-macropad firmware for the Adafruit MacroPad RP2040 (CircuitPython 9.x).

The pad is deliberately dumb, like the Codex Micro: it reports key and encoder
events to the host over the USB *data* serial port as JSON lines, and paints
whatever LED colors and OLED text the host daemon sends back. All Claude and
tmux logic lives in daemon/macropad_daemon.py. The one exception is
push-to-talk, which is sent as a real HID keystroke from the pad so it keeps
working even when the daemon is down.

Wire protocol (newline-delimited JSON):
  pad  -> host  {"t":"key","n":<0-11>,"a":"p"|"r"}
                {"t":"enc","d":<signed delta>}
                {"t":"encsw","a":"p"|"r"}
                {"t":"hi","fw":FW_VERSION,"rows":N,"cols":N}   reply to hello
  host -> pad   {"t":"hello"}
                {"t":"leds","c":[[r,g,b], ... x12]}
                {"t":"oled","l":["line", ...]}                  up to rows
                {"t":"hid","k":["SPACE"],"a":"p"|"r"}           host-driven keystrokes

Requires the adafruit_macropad library bundle:  circup install adafruit_macropad
"""

import json
import time

import terminalio
import usb_cdc
from adafruit_hid.keycode import Keycode
from adafruit_macropad import MacroPad

FW_VERSION = "0.4"
# Optional push-to-talk key: held -> HID Space held (Claude Code voice dictation).
# None = disabled; the host owns all 12 keys.
PTT_KEY = None
# No traffic from the host for this long -> show the offline screen.
HOST_TIMEOUT_S = 5.0

macropad = MacroPad()
macropad.pixels.brightness = 0.25
macropad.pixels.auto_write = False

# No title: every row of the 128x64 panel is the host's to use.
text = macropad.display_text()
link = usb_cdc.data  # None until boot.py has run once after a hard reset


def count_rows():
    # SimpleTextDisplay allocates lines lazily on index, so indexing never raises;
    # count the lines whose glyph box still lands inside the panel instead.
    font_h = terminalio.FONT.get_bounding_box()[1]
    n = 0
    while n < 16 and text[n].y + font_h // 2 <= macropad.display.height:
        n += 1
    return n


ROWS = count_rows()
COLS = macropad.display.width // terminalio.FONT.get_bounding_box()[0]
text.show()  # once; re-assigning the root group on every update is another full refresh


def send(obj):
    if link is None:
        return
    try:
        link.write((json.dumps(obj) + "\n").encode())
    except Exception:
        pass


def set_lines(lines):
    # Assigning label.text rebuilds its glyphs even when unchanged, which blanks the row
    # for a frame; only touch rows whose content actually differs.
    for i in range(ROWS):
        want = lines[i] if i < len(lines) else ""
        if text[i].text != want:
            text[i].text = want


def show_offline():
    for i in range(12):
        macropad.pixels[i] = (0, 0, 0)
    if PTT_KEY is not None:
        macropad.pixels[PTT_KEY] = (0, 20, 20)
    macropad.pixels.show()
    if link is None:
        set_lines(["no data port", "hard-reset pad"])
    else:
        set_lines(["daemon offline"])


def handle(msg):
    kind = msg.get("t")
    if kind == "hello":
        send({"t": "hi", "fw": FW_VERSION, "rows": ROWS, "cols": COLS})
    elif kind == "leds":
        colors = msg.get("c", [])
        for i in range(min(12, len(colors))):
            macropad.pixels[i] = tuple(colors[i])
        macropad.pixels.show()
    elif kind == "oled":
        set_lines(msg.get("l", []))
    elif kind == "hid":
        codes = [getattr(Keycode, k) for k in msg.get("k", []) if hasattr(Keycode, k)]
        if not codes:
            return
        if msg.get("a") == "p":
            macropad.keyboard.press(*codes)
        else:
            macropad.keyboard.release(*codes)


show_offline()
buf = b""
last_rx = -HOST_TIMEOUT_S
offline = True
last_pos = macropad.encoder

while True:
    if link is not None and link.in_waiting:
        buf += link.read(link.in_waiting)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                handle(json.loads(line))
            except ValueError:
                pass
            last_rx = time.monotonic()
            offline = False
        # A wedged host could otherwise grow buf without bound.
        if len(buf) > 4096:
            buf = b""

    event = macropad.keys.events.get()
    while event:
        if PTT_KEY is not None and event.key_number == PTT_KEY:
            # Blue while held = "Claude is listening"; the host repaints on release.
            if event.pressed:
                macropad.keyboard.press(Keycode.SPACE)
                macropad.pixels[PTT_KEY] = (0, 70, 255)
            else:
                macropad.keyboard.release(Keycode.SPACE)
                macropad.pixels[PTT_KEY] = (0, 0, 0)
            macropad.pixels.show()
        send({"t": "key", "n": event.key_number, "a": "p" if event.pressed else "r"})
        event = macropad.keys.events.get()

    pos = macropad.encoder
    if pos != last_pos:
        send({"t": "enc", "d": pos - last_pos})
        last_pos = pos

    macropad.encoder_switch_debounced.update()
    if macropad.encoder_switch_debounced.pressed:
        send({"t": "encsw", "a": "p"})
    if macropad.encoder_switch_debounced.released:
        send({"t": "encsw", "a": "r"})

    if not offline and time.monotonic() - last_rx > HOST_TIMEOUT_S:
        offline = True
        show_offline()

    time.sleep(0.005)
