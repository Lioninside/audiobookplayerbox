#!/usr/bin/env python3
"""
Button Index Finder
===================
Run this on the Pi to discover how the USB controller reports button presses.
Some encoders send joystick button events; others send keyboard key events.
Press a button — whatever the controller sends will print immediately.

Usage:
    python3 test_buttons.py

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
print("Press any button — what the controller sends will be shown.")
print("Press Ctrl+C to quit.\n")

try:
    while True:
        for event in pygame.event.get():
            if event.type == pygame.JOYBUTTONDOWN:
                print(f"  --> JOYSTICK button index: {event.button}")
            elif event.type == pygame.JOYAXISMOTION:
                print(f"  --> JOYSTICK axis {event.axis} = {event.value:.2f}")
            elif event.type == pygame.JOYHATMOTION:
                print(f"  --> JOYSTICK hat {event.hat} = {event.value}")
            elif event.type == pygame.KEYDOWN:
                print(f"  --> KEYBOARD key: {pygame.key.name(event.key)} (scancode {event.scancode})")
        time.sleep(0.02)
except KeyboardInterrupt:
    print("\nDone.")
finally:
    pygame.quit()
