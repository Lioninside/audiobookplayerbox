#!/usr/bin/env python3
"""
Button Index Finder
===================
Run this on the Pi to discover which joystick button index each physical
button reports. Press a button — the index prints immediately.

Usage:
    python3 test_buttons.py

Once you know all indices, update the BTN_* constants in player.py.
Press Ctrl+C to quit.
"""

import os
import time

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame

pygame.init()
pygame.joystick.init()

if pygame.joystick.get_count() == 0:
    print("ERROR: No joystick/gamepad found. Is the USB controller plugged in?")
    raise SystemExit(1)

joy = pygame.joystick.Joystick(0)
joy.init()
print(f"Controller: '{joy.get_name()}' | {joy.get_numbuttons()} button(s)")
print("Press any button — index will be shown.")
print("Press Ctrl+C to quit.\n")

prev = [0] * joy.get_numbuttons()

try:
    while True:
        pygame.event.pump()
        for i in range(joy.get_numbuttons()):
            cur = joy.get_button(i)
            if cur == 1 and prev[i] == 0:
                print(f"  --> Button index: {i}")
            prev[i] = cur
        time.sleep(0.02)
except KeyboardInterrupt:
    print("\nDone.")
finally:
    pygame.quit()
