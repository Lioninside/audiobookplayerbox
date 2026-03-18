#!/usr/bin/env python3
"""
Audiobook Player — Raspberry Pi Arcade Box
===========================================

Audio:  VLC
Input:  USB joystick / gamepad (pygame)

Button mapping (joystick index):
  0  Green         Play / Pause
                   On resume: rewinds RESUME_BACK_SEC seconds first
                   so the listener gets context after a break.
  1  Yellow        Next audiobook (starts playing immediately)
  2  Blue          Previous audiobook (starts playing immediately)
  3  Yellow inner  Long press (≥ REBOOT_LONG_SEC s): reboot the Pi
  4  Black inner   Reset all saved positions
"""

import os
import sys
import time
import glob
import logging
import subprocess
import threading
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

AUDIOBOOKS_DIR         = "/home/radonthusis/audiobooks"
POS_DIR                = "/home/radonthusis/.audiobook_positions"
LOG_FILE               = "/home/radonthusis/audiobook_player.log"

POSITION_SAVE_INTERVAL = 10.0    # seconds between autosave during playback
RESUME_BACK_SEC        = 5       # seconds rewound each time play is pressed
REBOOT_LONG_SEC        = 3.0     # hold duration required to trigger reboot
DEBOUNCE_SEC           = 0.08    # per-edge debounce window
BTN_COOLDOWN           = 0.20    # minimum gap between fired actions per button
LOOP_SLEEP             = 0.010   # main-loop sleep (~100 Hz)

BTN_PLAY   = 0
BTN_NEXT   = 1
BTN_PREV   = 2
BTN_REBOOT = 3
BTN_RESET  = 4

# ── Headless SDL — pygame is used only for joystick, not for audio ────────────

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/xdg_runtime_dir")
try:
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)
except Exception:
    pass  # non-fatal; VLC does not require XDG_RUNTIME_DIR

# ── Logging ───────────────────────────────────────────────────────────────────

try:
    os.makedirs(POS_DIR, exist_ok=True)
except Exception as exc:
    print(f"WARNING: could not create POS_DIR {POS_DIR}: {exc}", file=sys.stderr)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Hardware imports ──────────────────────────────────────────────────────────

import pygame
import vlc

# ── Helper ────────────────────────────────────────────────────────────────────

def atomic_write(path: str, text: str) -> None:
    """Write text to path via a temp file; a crash never leaves a partial file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# ── Player ────────────────────────────────────────────────────────────────────

class AudiobookPlayer:

    def __init__(self):
        # ── Shared-state lock ─────────────────────────────────────────────────
        # Protects self.player, self.idx, self.paused, self.ended.
        # Acquired by the autosave thread AND the VLC event thread; the main
        # thread acquires it in every playback action.
        self._lock = threading.Lock()

        # ── Autosave stop signal ──────────────────────────────────────────────
        # Set during shutdown so Event.wait() returns immediately instead of
        # sleeping out the full POSITION_SAVE_INTERVAL. This eliminates the
        # race where the autosave thread and _shutdown() write the same .pos
        # file simultaneously.
        self._stop_event = threading.Event()

        self.books: list[str] = self._scan_books()
        if not self.books:
            log.error(f"No MP3 files found in {AUDIOBOOKS_DIR}")
            sys.exit(1)

        self.idx      = 0
        self.paused   = True
        self.ended    = False
        self.length_s = 0.0

        # One persistent VLC instance; individual MediaPlayer objects are
        # created per book and explicitly released when done.
        self._vlc    = vlc.Instance("--no-xlib", "--aout=alsa", "--file-caching=3000")
        self.player: vlc.MediaPlayer | None = None

        # Joystick — pygame initialised only for input, not for audio
        pygame.init()
        pygame.joystick.init()
        if pygame.joystick.get_count() == 0:
            log.error("No joystick/controller found")
            sys.exit(1)
        self.joy = pygame.joystick.Joystick(0)
        self.joy.init()
        nbtn = self.joy.get_numbuttons()
        log.info(f"Controller: '{self.joy.get_name()}' | {nbtn} button(s)")

        # Per-button tracking — all timestamps use time.monotonic() so they
        # are immune to NTP clock adjustments.
        # _btn_down uses None to distinguish "never pressed" from a real
        # timestamp: 0.0 would be seconds-since-boot away from 'now', which
        # could falsely satisfy the long-press threshold on the very first UP.
        self._btn_prev: list[int]               = [0]    * nbtn
        self._btn_edge: list[float]             = [0.0]  * nbtn
        self._btn_fire: list[float]             = [0.0]  * nbtn
        self._btn_down: list[float | None]      = [None] * nbtn

        # Autosave daemon thread
        threading.Thread(
            target=self._autosave_loop, daemon=True, name="autosave"
        ).start()

        self.idx = self._restore_index()
        self._load_book(self.idx, start_paused=True)
        log.info("Ready — press GREEN to play")

    # ── Book list ─────────────────────────────────────────────────────────────

    def _scan_books(self) -> list[str]:
        return sorted(
            glob.glob(os.path.join(AUDIOBOOKS_DIR, "*.mp3")),
            key=lambda p: Path(p).name.lower(),
        )

    # ── Position persistence ──────────────────────────────────────────────────

    def _pos_file(self, path: str) -> str:
        return os.path.join(POS_DIR, Path(path).name + ".pos")

    def _index_file(self) -> str:
        return os.path.join(POS_DIR, "_current_index.txt")

    def _restore_index(self) -> int:
        try:
            with open(self._index_file()) as f:
                return int(f.read().strip()) % len(self.books)
        except Exception:
            return 0

    def _save_index(self) -> None:
        atomic_write(self._index_file(), str(self.idx))

    def _save_pos(self) -> None:
        """Persist current playback position.

        Must be called with self._lock already held (either by the caller
        directly, or transitively through _load_book / _autosave_loop /
        _shutdown). Doing the write inside the lock keeps the file access
        serialised across threads.
        """
        if self.player is None:
            return
        ms = self.player.get_time()
        if ms is None or ms < 0:
            return
        try:
            atomic_write(self._pos_file(self.books[self.idx]), f"{ms / 1000.0:.3f}")
        except OSError as exc:
            log.error(f"Could not save position: {exc}")

    def _load_pos(self, path: str) -> float:
        try:
            with open(self._pos_file(path)) as f:
                return float(f.read().strip())
        except Exception:
            return 0.0

    def _reset_all_positions(self) -> None:
        count = 0
        for f in glob.glob(os.path.join(POS_DIR, "*.pos")):
            try:
                os.remove(f)
                count += 1
            except OSError as exc:
                log.warning(f"Could not remove {f}: {exc}")
        log.info(f"Reset {count} position file(s)")

    def _autosave_loop(self) -> None:
        """Periodic position save in a daemon thread.

        Uses Event.wait() instead of time.sleep() so _shutdown() can wake
        this thread immediately by setting _stop_event, avoiding a race where
        both this thread and _shutdown() write the .pos file at the same time.
        """
        while not self._stop_event.wait(POSITION_SAVE_INTERVAL):
            with self._lock:
                if not self.paused and not self.ended:
                    self._save_pos()

    # ── Book loading ──────────────────────────────────────────────────────────

    def _load_book(self, index: int, start_paused: bool) -> None:
        """Stop any current playback, load a new book, and optionally start it."""
        with self._lock:
            # ── Tear down old player ──────────────────────────────────────────
            if self.player is not None:
                self._save_pos()
                try:
                    self.player.stop()
                except Exception as exc:
                    log.debug(f"player.stop() during teardown: {exc}")
                try:
                    self.player.release()
                except Exception as exc:
                    log.warning(f"player.release() failed (VLC handle may leak): {exc}")
                self.player = None

            # Refresh book list in case files changed on disk
            self.books = self._scan_books()
            if not self.books:
                log.error("No books found on disk")
                return

            self.idx    = index % len(self.books)
            path        = self.books[self.idx]
            last_s      = self._load_pos(path)
            self.ended  = False
            self.paused = start_paused

            # ── Set up new player ─────────────────────────────────────────────
            self.player = self._vlc.media_player_new()
            m = self._vlc.media_new(path)
            m.add_option(":input-fast-seek")
            m.add_option(":file-caching=3000")
            self.player.set_media(m)

            em = self.player.event_manager()
            em.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_end)

            # VLC must reach Playing/Paused before a seek is meaningful.
            # Mute briefly to avoid an audible click when we pause immediately.
            self.player.audio_set_volume(0)
            self.player.play()

            # Wait up to 2 s for the player to become ready.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                state = self.player.get_state()
                if state in (vlc.State.Playing, vlc.State.Paused):
                    break
                if state == vlc.State.Error:
                    log.error(f"VLC error opening: {path}")
                    return
                time.sleep(0.05)
            else:
                log.error(f"VLC timed out opening: {path}")
                return

            if last_s > 0:
                self.player.set_time(int(last_s * 1000))

            if start_paused:
                self.player.pause()
            # else: already playing

            self.player.audio_set_volume(100)

            # Duration via mutagen (reads file header, not VLC's stream probe)
            try:
                from mutagen.mp3 import MP3
                self.length_s = MP3(path).info.length
            except Exception:
                raw = self.player.get_length()
                self.length_s = raw / 1000.0 if raw and raw > 0 else 0.0

            self._save_index()
            log.info(
                f"Book: {Path(path).name} | "
                f"pos={last_s:.1f}s | len={self.length_s:.1f}s | "
                f"{'paused' if start_paused else 'playing'}"
            )

    # ── VLC end-of-media callback (runs on a VLC internal thread) ─────────────

    def _on_end(self, _event) -> None:
        with self._lock:
            if self.player is None:
                return
            # Do NOT call _save_pos() here: at MediaPlayerEndReached, get_time()
            # returns the total duration. Saving that causes the next load to
            # seek to the very end and immediately fire EndReached again →
            # infinite loop. Write 0 so the next play starts from the beginning.
            try:
                atomic_write(self._pos_file(self.books[self.idx]), "0.000")
            except OSError as exc:
                log.error(f"Could not reset position on end: {exc}")
            self.ended  = True
            self.paused = False
        log.info(f"Finished: {Path(self.books[self.idx]).name}")

    # ── Playback actions (called from main thread only) ───────────────────────

    def _toggle_play(self) -> None:
        # Snapshot shared state under the lock in a single acquisition.
        # Previously self.ended was read inside the lock (to get idx) and then
        # read again outside it — a data race.
        with self._lock:
            if self.player is None:
                return
            ended = self.ended
            idx   = self.idx

        if ended:
            # A VLC player in State.Ended cannot be restarted via set_time()+
            # play(). Reload the media from the top. _load_book acquires the
            # lock internally, so it must be called outside our lock scope.
            self._load_book(idx, start_paused=False)
            return

        with self._lock:
            if self.paused:
                # Rewind a few seconds before resuming so the listener
                # gets context after a break — especially useful for
                # users who may have drifted off mid-sentence.
                cur_ms = self.player.get_time() or 0
                tgt_ms = max(0, cur_ms - RESUME_BACK_SEC * 1000)
                self.player.set_time(tgt_ms)
                self.player.play()
                self.paused = False
                log.info(f"Play (rewound {RESUME_BACK_SEC}s → {tgt_ms / 1000.0:.1f}s)")
            else:
                self.player.pause()
                self.paused = True
                self._save_pos()
                log.info("Pause")

    def _next_book(self) -> None:
        with self._lock:
            idx = (self.idx + 1) % len(self.books)
        self._load_book(idx, start_paused=False)
        log.info("Next book")

    def _prev_book(self) -> None:
        with self._lock:
            idx = (self.idx - 1) % len(self.books)
        self._load_book(idx, start_paused=False)
        log.info("Previous book")

    def _reboot(self) -> None:
        log.info("Rebooting…")
        with self._lock:
            self._save_pos()
        # subprocess.run with a list avoids shell interpretation and gives a
        # clean returncode; stderr is captured for logging if the command fails.
        result = subprocess.run(
            ["sudo", "/sbin/reboot"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            log.error(
                f"Reboot failed (rc={result.returncode}): "
                f"{result.stderr.strip() or '(no output)'}"
            )

    # ── Button polling (~100 Hz) ──────────────────────────────────────────────

    def _poll_buttons(self) -> None:
        # time.monotonic() for all button timing: immune to NTP clock jumps.
        # With time.time(), an NTP correction of a few seconds at startup
        # could make a held BTN_BLK appear long-pressed and trigger a reboot.
        now = time.monotonic()
        pygame.event.pump()

        for btn in range(self.joy.get_numbuttons()):
            cur  = self.joy.get_button(btn)
            prev = self._btn_prev[btn]

            if cur == prev:
                continue

            # Edge detected — only act if outside the debounce window
            if now - self._btn_edge[btn] < DEBOUNCE_SEC:
                self._btn_prev[btn] = cur
                continue

            self._btn_edge[btn] = now
            self._btn_prev[btn] = cur

            if cur == 1:
                # ── DOWN ──────────────────────────────────────────────────────
                self._btn_down[btn] = now
                if now - self._btn_fire[btn] < BTN_COOLDOWN:
                    continue
                if btn == BTN_PLAY:
                    self._toggle_play();  self._btn_fire[btn] = now
                elif btn == BTN_NEXT:
                    self._next_book();    self._btn_fire[btn] = now
                elif btn == BTN_PREV:
                    self._prev_book();    self._btn_fire[btn] = now
                elif btn == BTN_RESET:
                    self._reset_all_positions(); self._btn_fire[btn] = now
                # BTN_REBOOT: decision deferred to release (long-press guard)

            else:
                # ── UP ────────────────────────────────────────────────────────
                if btn == BTN_REBOOT:
                    if now - self._btn_fire[btn] < BTN_COOLDOWN:
                        continue
                    down_t = self._btn_down[btn]
                    if down_t is None:
                        # UP without a matching DOWN (Pi booted while button
                        # was held). Ignore to prevent a false reboot.
                        continue
                    if now - down_t >= REBOOT_LONG_SEC:
                        self._reboot()
                        self._btn_fire[btn] = now

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        log.info("=== Audiobook player started ===")
        try:
            while True:
                self._poll_buttons()
                time.sleep(LOOP_SLEEP)
        except KeyboardInterrupt:
            log.info("Interrupted")
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        # Signal the autosave thread to stop immediately; this prevents a race
        # where autosave writes the .pos file at the same time as _save_pos()
        # below.
        self._stop_event.set()

        with self._lock:
            self._save_pos()
            if self.player is not None:
                try:
                    self.player.stop()
                except Exception as exc:
                    log.debug(f"player.stop() during shutdown: {exc}")
                try:
                    self.player.release()
                except Exception as exc:
                    log.warning(f"player.release() during shutdown: {exc}")
        try:
            pygame.quit()
        except Exception as exc:
            log.debug(f"pygame.quit() during shutdown: {exc}")
        log.info("Shutdown complete")


# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    AudiobookPlayer().run()
