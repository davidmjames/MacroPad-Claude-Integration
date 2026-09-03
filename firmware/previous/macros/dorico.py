# SPDX-FileCopyrightText: 2021 Emma Humphries for Adafruit Industries
#
# SPDX-License-Identifier: MIT

# Dorico note entry shortcuts

from adafruit_hid.keycode import Keycode # REQUIRED if using Keycode.* values

app = {                # REQUIRED dict, must be named 'app'
    'name' : 'Dorico', # Application name
    'macros' : [       # List of button macros...
        # COLOR    LABEL    KEY SEQUENCE
        # 1st row ----------
        (0x202000, '1/2', ['7']),
        (0x202000, '1', ['8']),
        (0x202000, '2', ['9']),
        # 2nd row ----------
        (0x202000, '1/16', ['4']),
        (0x202000, '1/8', ['5']),
        (0x202000, '1/4', ['6']),
        # 3rd row ----------
        (0x202000, '.', ['.']),
        (0x202000, 'tie', ['t']),
        (0x202000, '', ['-']),
        # 4th row ----------
        (0x202000, '#', ['=']),
        (0x202000, 'n', ['0']),
        (0x202000, 'gr', ['/']),
        # Encoder button ---
        (0x000000, '', [Keycode.BACKSPACE])
    ]
}
