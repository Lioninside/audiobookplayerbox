# Audiobook Player Box

> A Raspberry Pi audiobook player built for seniors — large arcade buttons, zero menus, just press play.

Built for a visually impaired mother who loves audiobooks but struggles with smartphones, voice assistants, and the expensive "accessible" devices that are still too complicated. The design goal was the simplicity of a cassette deck from the 1980s: one big button to play, done.

The enclosure is a wooden box with five colour-coded arcade buttons wired to a USB gamepad controller. A Raspberry Pi inside runs VLC, saves your position automatically, and starts playing again after a reboot — no screen, no menus, no app.

Read the full story: [DIY Hörbuchplayer mit Arcade-Buttons — lioninside.com](https://lioninside.com/meine-geschichte/diy-hoerbuchplayer-mit-arcade-buttons-einfache-bedienung-fuer-senioren/)

---

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi (any model with audio out) | Tested on Pi 3 B+ |
| USB arcade gamepad / joystick controller | Zero-delay encoder from any arcade parts supplier |
| 5 arcade buttons | Large 60 mm or standard 30 mm, different colours |
| Wooden box or enclosure | Old cigar box, project box, whatever fits |
| Speakers or 3.5 mm audio output | ALSA / direct HDMI audio |

The USB controller is plug-and-play on Raspberry Pi OS — no drivers needed.

---

## Button layout

| Button | Colour | Action |
|---|---|---|
| 0 | Green (large, centre) | **Play / Pause** — on resume, rewinds 5 seconds for context |
| 1 | Yellow | Next audiobook (starts playing immediately) |
| 2 | Blue | Previous audiobook (starts playing immediately) |
| 3 | Yellow inner | Long press (≥ 3 s): reboot the Pi |
| 4 | Black inner | Reset all saved positions |

Button indices are zero-based joystick button numbers as reported by pygame. If your controller has a different mapping, adjust the `BTN_*` constants at the top of `player.py`.

---

## How it works

- MP3 files live in `~/audiobooks/`. Drop files there via USB drive, `scp`, or Samba.
- On every play and every 10 seconds during playback the current position is saved to `~/.audiobook_positions/`. The Pi can be switched off at any time — playback resumes exactly where it stopped.
- Pressing Play rewinds 5 seconds before resuming so the listener gets context after a break.
- Books are sorted alphabetically. Next and Previous cycle through the list.
- When a book finishes its position resets to zero so the next play starts from the beginning.
- The player starts automatically at boot via systemd and restarts itself after any crash.

### Voice reminder

Between 10:00 and 19:30, if no button has been pressed for 5 hours and playback is paused, the box speaks a short German reminder:

> *„Hallo! Wie wäre es mit einem Hörbuch? Drücke den grünen Knopf zum Starten. Der grüne Knopf ist oben. Du hörst gerade: [book name]."*

The reminder is generated offline by `espeak-ng` (no internet required) and played through the same speaker. Any button press stops it immediately. If the reminder is ignored, it repeats once every 5 hours within the active window.

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/Lioninside/audiobookplayerbox.git
cd audiobookplayerbox
```

### 2. Run the install script (as root)

```bash
sudo bash install.sh
```

This will:
- Install `vlc`, `python3-pygame`, `python3-mutagen`, `python-vlc`, and `espeak-ng`
- Create `~/audiobooks/`
- Install and enable the systemd service so the player starts at boot

### 3. Copy your audiobooks

```bash
# From your computer:
scp mybook.mp3 pi@raspberrypi:~/audiobooks/

# Or copy from a USB drive:
cp /media/usb/*.mp3 ~/audiobooks/
```

### 4. Check the button indices (if needed)

Run the test script to see which button index your controller reports for each physical button:

```bash
python3 test_buttons.py
```

Adjust `BTN_PLAY`, `BTN_NEXT`, `BTN_PREV`, `BTN_REBOOT`, `BTN_RESET` at the top of `player.py` if needed, then restart the service:

```bash
sudo systemctl restart audiobook
```

---

## Joystick / gamepad detection

pygame scans all USB HID devices and may expose keyboards, USB hubs, or other peripherals as joysticks with an unusually high button count (20+). These devices never fire real button events and must not be mistaken for the arcade controller.

The player handles this automatically: it skips any device that reports more than 16 buttons and uses the first device that reports 16 or fewer. Standard arcade zero-delay encoders report 8–12 buttons, so they are selected correctly. You can confirm which device was chosen in the log at startup:

```
Controller: 'USB Gamepad' | 8 button(s)
```

If you see the line `Skipping '...' (N buttons) — not a gamepad` followed by no controller being found, the arcade encoder is either not plugged in or it also reports more than 16 buttons (unusual but possible with some encoders). In that case:

1. Run `python3 test_buttons.py` with only the arcade controller connected to identify its exact name and button count.
2. Adjust the `<= 16` threshold in `player.py` if your encoder genuinely uses more than 16 buttons.

If pygame picks the wrong device (e.g. a keyboard before the encoder), unplug all non-gamepad USB peripherals and restart the service, or re-order the USB connections.

---

## Configuration

All tuneable constants are at the top of `player.py`:

| Constant | Default | Description |
|---|---|---|
| `AUDIOBOOKS_DIR` | `~/audiobooks` | Where MP3 files are scanned |
| `POS_DIR` | `~/.audiobook_positions` | Where positions are saved |
| `RESUME_BACK_SEC` | `5` | Seconds rewound on each resume |
| `REBOOT_LONG_SEC` | `3.0` | Hold duration to trigger reboot |
| `POSITION_SAVE_INTERVAL` | `10.0` | Autosave interval in seconds |
| `REMINDER_INACTIVITY_SEC` | `18000` (5 h) | Inactivity before reminder fires |
| `REMINDER_COOLDOWN_SEC` | `7200` (2 h) | Minimum gap between reminders |
| `REMINDER_WINDOW_START` | `600` (10:00) | Earliest reminder (minutes since midnight) |
| `REMINDER_WINDOW_END` | `1170` (19:30) | Latest reminder (minutes since midnight) |
| `REMINDER_SPEECH_RATE` | `120` | espeak-ng words per minute |

---

## Useful commands

```bash
# Watch the live log
journalctl -u audiobook -f

# Restart the player
sudo systemctl restart audiobook

# Stop the player
sudo systemctl stop audiobook

# Disable autostart
sudo systemctl disable audiobook
```

---

## Requirements

- Raspberry Pi OS (Bookworm or Bullseye)
- Python 3.9+
- `vlc`, `python-vlc`, `pygame`, `mutagen`, `espeak-ng`

All installed automatically by `install.sh`.

---

## License

MIT
