#!/usr/bin/env python3
"""
Button Pin Finder
=================
Run this on the Pi to discover which GPIO pin each button is wired to.
Press a button — the BCM pin number prints immediately.

Usage:
    python3 test_buttons.py

Once you know all pin numbers, update the BTN_* constants in player.py.
Press Ctrl+C to quit.
"""

import time

try:
    import RPi.GPIO as GPIO
except ImportError:
    print("ERROR: RPi.GPIO not found. Run:  sudo apt install python3-rpi.gpio")
    raise SystemExit(1)

ALL_PINS = [2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27]

GPIO.setmode(GPIO.BCM)
for pin in ALL_PINS:
    try:
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    except Exception:
        pass

print("Press any button — GPIO pin number will be shown.")
print("Press Ctrl+C to quit.\n")

try:
    while True:
        for pin in ALL_PINS:
            try:
                if GPIO.input(pin) == GPIO.LOW:
                    print(f"  --> GPIO {pin} (BCM)")
                    time.sleep(0.4)
            except Exception:
                pass
        time.sleep(0.02)
except KeyboardInterrupt:
    print("\nDone.")
finally:
    GPIO.cleanup()
