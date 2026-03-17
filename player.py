#!/usr/bin/env python3
"""
Audiobook Player for Raspberry Pi Arcade Box
=============================================

Buttons (BCM GPIO, internal pull-up, press = LOW):
  Green  (GPIO 17) — Play / Pause
  Blue   (GPIO 27) — Next audiobook (advances and starts playing)
  Yellow (GPIO 22) — [placeholder — reserved for future feature]
  Black  (GPIO  5) — Restart application
  Yellow (GPIO  6) — [placeholder — reserved for future feature]

Volume is handled externally via a USB volume controller.

Audiobooks: single MP3 files in AUDIOBOOKS_DIR, sorted alphabetically.
Playback position is saved every POSITION_SAVE_INTERVAL seconds and on
every pause/stop, so the player resumes exactly where you left off after
a crash or power cut.

Autostart: managed by audiobook.service (systemd, Restart=always).
"""

import os
import sys
import time
import json
import glob
import logging
import threading
from pathlib import Path

# ─── CONFIGURATION (edit these to match your hardware) ────────────────────────

AUDIOBOOKS_DIR         = "/home/radonthusis/audiobooks"
STATE_FILE             = "/home/radonthusis/.audiobook_player_state.json"
LOG_FILE               = "/home/radonthusis/audiobook_player.log"

POSITION_SAVE_INTERVAL = 10    # seconds between automatic position saves

# GPIO pin numbers — BCM mode, change to match your actual wiring
PIN_GREEN          = 17   # Play / Pause
PIN_BLUE           = 27   # Next audiobook
PIN_YELLOW_SIDE    = 22   # [future use]
PIN_BLACK_INSIDE   = 5    # Restart app
PIN_YELLOW_INSIDE  = 6    # [future use]

DEBOUNCE_MS        = 300  # hardware debounce time passed to GPIO library
DEBOUNCE_S         = 0.35 # software guard (slightly longer than hardware)

# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── Optional hardware imports (fall back gracefully for dev/testing) ──────────

try:
    import RPi.GPIO as GPIO
    _GPIO_OK = True
except ImportError:
    log.warning("RPi.GPIO not found — running in GPIO simulation mode")
    _GPIO_OK = False

try:
    import pygame
    pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=4096)
    pygame.mixer.init()
    pygame.init()
    _PYGAME_OK = True
    log.info("pygame mixer initialised")
except Exception as exc:
    log.error(f"pygame init failed: {exc}")
    _PYGAME_OK = False


# ──────────────────────────────────────────────────────────────────────────────

class AudiobookPlayer:
    """Thread-safe audiobook player with persistent resume support."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_press: dict[int, float] = {}

        self.books: list[str] = []
        self.current_index: int = 0
        self.is_playing: bool = False
        self._position: float = 0.0          # seconds; updated on pause/stop
        self._play_started_at: float = 0.0   # time.monotonic() snapshot

        self._load_books()
        self._load_state()
        self._setup_gpio()
        self._start_position_saver()

    # ── Book list ─────────────────────────────────────────────────────────────

    def _load_books(self):
        files = sorted(glob.glob(os.path.join(AUDIOBOOKS_DIR, "*.mp3")))
        self.books = files
        if files:
            log.info(f"Found {len(files)} book(s): {[Path(b).stem for b in files]}")
        else:
            log.warning(f"No MP3 files found in {AUDIOBOOKS_DIR}")

    # ── State persistence ─────────────────────────────────────────────────────

    def _load_state(self):
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            book = state.get("book", "")
            if book in self.books:
                self.current_index = self.books.index(book)
                self._position = float(state.get("position", 0.0))
                log.info(f"Resumed state: {Path(book).stem} @ {self._position:.1f}s")
            else:
                log.info("Saved state book not found in library — starting fresh")
        except (FileNotFoundError, json.JSONDecodeError):
            log.info("No saved state found — starting from the beginning")
        except Exception as exc:
            log.error(f"Error loading state: {exc}")

    def _save_state(self):
        if not self.books:
            return
        pos = self._current_position()
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"book": self.books[self.current_index], "position": pos}, f)
            os.replace(tmp, STATE_FILE)  # atomic write — safe on crash
        except Exception as exc:
            log.error(f"Failed to save state: {exc}")

    def _start_position_saver(self):
        """Background thread: save position periodically during playback."""
        def _loop():
            while True:
                time.sleep(POSITION_SAVE_INTERVAL)
                if self.is_playing:
                    self._save_state()
        threading.Thread(target=_loop, daemon=True, name="position-saver").start()

    # ── Position tracking ─────────────────────────────────────────────────────

    def _current_position(self) -> float:
        """Return current playback position in seconds."""
        if self.is_playing:
            return self._position + (time.monotonic() - self._play_started_at)
        return self._position

    # ── Playback controls ─────────────────────────────────────────────────────

    def _book_name(self) -> str:
        return Path(self.books[self.current_index]).stem if self.books else "—"

    def play(self):
        """Start/resume playback of the current book from saved position."""
        if not self.books:
            log.warning("No books to play")
            return

        book = self.books[self.current_index]

        if not _PYGAME_OK:
            log.info(f"[SIM] play '{self._book_name()}' from {self._position:.1f}s")
            self.is_playing = True
            self._play_started_at = time.monotonic()
            return

        try:
            pygame.mixer.music.load(book)
            pygame.mixer.music.play(start=self._position)
            self.is_playing = True
            self._play_started_at = time.monotonic()
            log.info(f"Playing: '{self._book_name()}' from {self._position:.1f}s")
        except Exception as exc:
            log.error(f"Playback error: {exc}")
            self.is_playing = False

    def pause(self):
        """Pause playback and persist position."""
        if not self.is_playing:
            return
        self._position = self._current_position()
        self.is_playing = False
        if _PYGAME_OK:
            try:
                pygame.mixer.music.stop()
            except Exception as exc:
                log.error(f"Stop error: {exc}")
        self._save_state()
        log.info(f"Paused '{self._book_name()}' at {self._position:.1f}s")

    def toggle_play_pause(self):
        with self._lock:
            if self.is_playing:
                self.pause()
            else:
                self.play()

    def next_book(self):
        """Stop current book, advance to next, and start playing it."""
        with self._lock:
            if not self.books:
                return
            self.pause()
            self.current_index = (self.current_index + 1) % len(self.books)
            self._position = 0.0
            self._save_state()
            log.info(f"Next book: '{self._book_name()}'")
            self.play()

    def restart_app(self):
        """Save state and re-exec this process (systemd will see a new PID)."""
        log.info("Restarting application…")
        self.pause()
        self._save_state()
        logging.shutdown()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # ── GPIO setup ────────────────────────────────────────────────────────────

    def _debounced(self, pin: int) -> bool:
        """Software guard: returns True only if DEBOUNCE_S has elapsed."""
        now = time.monotonic()
        if now - self._last_press.get(pin, 0.0) < DEBOUNCE_S:
            return False
        self._last_press[pin] = now
        return True

    def _setup_gpio(self):
        if not _GPIO_OK:
            log.info("GPIO not available — skipping hardware setup")
            return

        GPIO.cleanup()          # clear any stale event-detect from a previous run
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

        pins = [PIN_GREEN, PIN_BLUE, PIN_YELLOW_SIDE, PIN_BLACK_INSIDE, PIN_YELLOW_INSIDE]
        for pin in pins:
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        time.sleep(0.1)         # let the kernel settle before arming edge detection

        callbacks = {
            PIN_GREEN:          self._cb_green,
            PIN_BLUE:           self._cb_blue,
            PIN_YELLOW_SIDE:    self._cb_yellow_side,
            PIN_BLACK_INSIDE:   self._cb_black_inside,
            PIN_YELLOW_INSIDE:  self._cb_yellow_inside,
        }
        for pin, cb in callbacks.items():
            GPIO.remove_event_detect(pin)   # ensure no duplicate listeners
            GPIO.add_event_detect(pin, GPIO.FALLING, callback=cb, bouncetime=DEBOUNCE_MS)

        log.info("GPIO configured with internal pull-ups")

    # ── Button callbacks ──────────────────────────────────────────────────────

    def _cb_green(self, pin):
        if self._debounced(pin):
            log.info("Button: GREEN (play/pause)")
            self.toggle_play_pause()

    def _cb_blue(self, pin):
        if self._debounced(pin):
            log.info("Button: BLUE (next book)")
            self.next_book()

    def _cb_yellow_side(self, pin):
        if self._debounced(pin):
            log.info("Button: YELLOW SIDE (not yet assigned)")
            # TODO: assign feature (e.g. volume up, rewind 30s…)

    def _cb_black_inside(self, pin):
        if self._debounced(pin):
            log.info("Button: BLACK INSIDE (restart app)")
            self.restart_app()

    def _cb_yellow_inside(self, pin):
        if self._debounced(pin):
            log.info("Button: YELLOW INSIDE (not yet assigned)")
            # TODO: assign feature (e.g. volume down, fast-forward 30s…)

    # ── Natural end-of-book detection ─────────────────────────────────────────

    def _check_book_finished(self):
        """Called from main loop: advance to next book when current one ends."""
        if not _PYGAME_OK or not self.is_playing:
            return
        if not pygame.mixer.music.get_busy():
            log.info(f"'{self._book_name()}' finished — advancing")
            with self._lock:
                self.is_playing = False
                self._position = 0.0
                self.current_index = (self.current_index + 1) % len(self.books)
                self._save_state()
                self.play()

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        log.info("=== Audiobook player started ===")
        if self.books:
            log.info(f"Ready: '{self._book_name()}' (press GREEN to play)")
        else:
            log.warning(f"No MP3 files in {AUDIOBOOKS_DIR} — add some and restart")

        try:
            while True:
                self._check_book_finished()
                time.sleep(0.5)
        except KeyboardInterrupt:
            log.info("Interrupted — shutting down")
        finally:
            self._cleanup()

    def _cleanup(self):
        log.info("Cleaning up…")
        self.pause()
        self._save_state()
        if _GPIO_OK:
            GPIO.cleanup()
        if _PYGAME_OK:
            try:
                pygame.mixer.quit()
                pygame.quit()
            except Exception:
                pass
        log.info("Shutdown complete")


# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    player = AudiobookPlayer()
    player.run()
