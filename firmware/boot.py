# Enables the second USB serial port (usb_cdc.data) that the daemon talks to.
# The console port stays available for the REPL / print() debugging.
# boot.py only runs at power-on or hard reset, not on a soft reload (Ctrl-D).
import usb_cdc

usb_cdc.enable(console=True, data=True)
