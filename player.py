#!/usr/bin/env python3
"""
Audiobook Player — Raspberry Pi Arcade Box
===========================================

Audio:  VLC
Input:  USB joystick / gamepad (pygame)

Button mapping (joystick index):
  0  Green         Play / Pause
  1  Yellow outer  +60 s seek
  2  Blue outer    −60 s seek
  3  Yellow inner  Next audiobook (starts playing)
  4  Black inner   Short press (<3 s): reset all saved positions
                   Long  press (≥3 s): reboot
"""

import os
import sys
import time
import glob
import logging
import threading
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

AUDIOBOOKS_DIR         = "/home/radonthusis/audiobooks"
POS_DIR                = "/home/radonthusis/.audiobook_positions"
LOG_FILE               = "/home/radonthusis/audiobook_player.log"

POSITION_SAVE_INTERVAL = 10.0    # seconds between autosave during playback
JUMP_SECONDS           = 60      # seek step size in seconds
BLACK_LONG_SEC         = 3.0     # hold duration threshold for reboot
DEBOUNCE_SEC           = 0.08    # per-edge debounce window
BTN_COOLDOWN           = 0.20    # minimum gap between fired actions per button
LOOP_SLEEP             = 0.010   # main-loop sleep (~100 Hz)

BTN_PLAY = 0
BTN_FWD  = 1
BTN_BACK = 2
BTN_NEXT = 3
BTN_BLK  = 4

# ── Headless SDL — pygame is used only for joystick, not for audio ────────────

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/xdg_runtime_dir")
try:
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)
except Exception:
    pass

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
    """Write text to path via a temp file so a crash never leaves a partial file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# ── Player ────────────────────────────────────────────────────────────────────

class AudiobookPlayer:

    def __init__(self):
        # Lock protecting self.player, self.idx, self.ended, self.paused.
        # Acquired by any thread that reads or writes these fields together.
        self._lock = threading.Lock()

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

        # Joystick — pygame is initialised only for input, not for audio
        pygame.init()
        pygame.joystick.init()
        if pygame.joystick.get_count() == 0:
            log.error("No joystick/controller found")
            sys.exit(1)
        self.joy = pygame.joystick.Joystick(0)
        self.joy.init()
        nbtn = self.joy.get_numbuttons()
        log.info(f"Controller: '{self.joy.get_name()}' | {nbtn} button(s)")

        # Per-button tracking (indexed by joystick button number)
        self._btn_prev = [0]   * nbtn   # last confirmed raw state
        self._btn_edge = [0.0] * nbtn   # timestamp of last processed edge
        self._btn_fire = [0.0] * nbtn   # timestamp of last fired action
        self._btn_down = [0.0] * nbtn   # timestamp of last DOWN event

        # Autosave runs in a daemon thread so it dies with the main process
        threading.Thread(target=self._autosave_loop, daemon=True, name="autosave").start()

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
        """Save current VLC playback position. Call only from main thread or
        with self._lock already held by the caller."""
        if self.player is None:
            return
        ms = self.player.get_time()
        if ms is None or ms < 0:
            return
        atomic_write(self._pos_file(self.books[self.idx]), f"{ms / 1000.0:.3f}")

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
            except Exception as e:
                log.warning(f"Could not remove {f}: {e}")
        log.info(f"Reset {count} position file(s)")

    def _autosave_loop(self) -> None:
        """Periodic position save. Runs in a daemon thread."""
        while True:
            time.sleep(POSITION_SAVE_INTERVAL)
            with self._lock:
                # Only save during active playback — not paused, not ended.
                if not self.paused and not self.ended:
                    self._save_pos()

    # ── Book loading ──────────────────────────────────────────────────────────

    def _load_book(self, index: int, start_paused: bool) -> None:
        """Stop any current playback, load a new book, and optionally start it."""
        with self._lock:
            # ── Tear down old player ──────────────────────────────────────────
            if self.player is not None:
                try:
                    self._save_pos()
                    self.player.stop()
                except Exception:
                    pass
                try:
                    # Bug fix: release the native VLC handle to avoid a leak.
                    self.player.release()
                except Exception:
                    pass
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

            # VLC must start playing before we can seek; we mute briefly to
            # avoid a click if we immediately pause afterward.
            self.player.audio_set_volume(0)
            self.player.play()

            # Wait up to 2 s for the player to become ready
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

            # Measure duration via mutagen (file header, not VLC stream probe)
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
            # Bug fix: do NOT save the current position here.
            # At EndReached, get_time() returns the total duration.
            # Saving that would cause the next load to seek to the very end
            # and immediately trigger EndReached again → infinite loop.
            # Instead, overwrite with 0 so the next play starts from the top.
            atomic_write(self._pos_file(self.books[self.idx]), "0.000")
            self.ended  = True
            self.paused = False
        log.info(f"Finished: {Path(self.books[self.idx]).name}")

    # ── Playback actions (called from main thread only) ───────────────────────

    def _toggle_play(self) -> None:
        with self._lock:
            if self.player is None:
                return
            if self.ended:
                # Bug fix: cannot resume a VLC player in State.Ended via
                # set_time()+play() — the player is exhausted. Reload the media.
                # _load_book acquires the same lock, so release first.
                idx = self.idx
        if self.ended:
            # ended=True path: reload book from beginning
            self._load_book(idx, start_paused=False)
            return

        with self._lock:
            if self.paused:
                self.player.play()
                self.paused = False
                log.info("Play")
            else:
                self.player.pause()
                self.paused = True
                self._save_pos()
                log.info("Pause")

    def _seek(self, delta_s: int) -> None:
        with self._lock:
            # Bug fix: seeking on an ended player is a silent no-op in VLC;
            # guard here to avoid confusing log entries and state corruption.
            if self.player is None or self.ended:
                return
            cur_ms = self.player.get_time() or 0
            max_ms = (
                int(self.length_s * 1000) if self.length_s > 0
                else (self.player.get_length() or 0)
            )
            tgt_ms = max(0, cur_ms + delta_s * 1000)
            if max_ms > 0:
                tgt_ms = min(tgt_ms, max(0, max_ms - 250))
            was_playing = (self.player.get_state() == vlc.State.Playing)
            self.player.set_time(tgt_ms)
            self._save_pos()
            # After set_time, VLC may briefly stall; restore state explicitly.
            if was_playing:
                self.player.play()
                self.paused = False
            else:
                self.player.pause()
                self.paused = True
            log.info(f"Seek {delta_s:+d}s → {tgt_ms / 1000.0:.1f}s")

    def _next_book(self) -> None:
        with self._lock:
            was_playing = not self.paused
            idx = (self.idx + 1) % len(self.books)
        self._load_book(idx, start_paused=not was_playing)
        log.info("Next book")

    def _reboot(self) -> None:
        log.info("Rebooting…")
        with self._lock:
            self._save_pos()
        rc = os.system("sudo /sbin/reboot")
        if rc != 0:
            log.error(f"Reboot command failed (rc={rc}) — check sudoers")

    # ── Button polling (~100 Hz) ──────────────────────────────────────────────

    def _poll_buttons(self) -> None:
        now = time.time()
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
                    self._toggle_play();        self._btn_fire[btn] = now
                elif btn == BTN_FWD:
                    self._seek(+JUMP_SECONDS);  self._btn_fire[btn] = now
                elif btn == BTN_BACK:
                    self._seek(-JUMP_SECONDS);  self._btn_fire[btn] = now
                elif btn == BTN_NEXT:
                    self._next_book();          self._btn_fire[btn] = now
                # BTN_BLK: decision deferred to release

            else:
                # ── UP ────────────────────────────────────────────────────────
                if btn == BTN_BLK:
                    if now - self._btn_fire[btn] < BTN_COOLDOWN:
                        continue
                    held = now - self._btn_down[btn]
                    if held >= BLACK_LONG_SEC:
                        self._reboot()
                    else:
                        self._reset_all_positions()
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
        with self._lock:
            self._save_pos()
            if self.player is not None:
                try:
                    self.player.stop()
                    self.player.release()
                except Exception:
                    pass
        try:
            pygame.quit()
        except Exception:
            pass
        log.info("Shutdown complete")


# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    AudiobookPlayer().run()
