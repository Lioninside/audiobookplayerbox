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
JUMP_SECONDS           = 60      # seek step size
BLACK_LONG_SEC         = 3.0     # hold time for long-press reboot
DEBOUNCE_SEC           = 0.08    # per-edge debounce
BTN_COOLDOWN           = 0.20    # minimum gap between actions on the same button
LOOP_SLEEP             = 0.010   # main-loop sleep (~100 Hz)

BTN_PLAY = 0
BTN_FWD  = 1
BTN_BACK = 2
BTN_NEXT = 3
BTN_BLK  = 4

# ── Headless SDL (no display, pygame used only for joystick) ──────────────────

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/xdg_runtime_dir")
try:
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)
except Exception:
    pass

os.makedirs(POS_DIR, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Late imports (require hardware libs) ─────────────────────────────────────

import pygame
import vlc

# ── Helper ────────────────────────────────────────────────────────────────────

def atomic_write(path: str, text: str):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# ── Player ────────────────────────────────────────────────────────────────────

class AudiobookPlayer:

    def __init__(self):
        self.books: list[str] = self._scan_books()
        if not self.books:
            log.error(f"No MP3 files found in {AUDIOBOOKS_DIR}")
            sys.exit(1)

        self.idx      = 0
        self.paused   = True
        self.ended    = False
        self.length_s = 0.0

        # VLC — dedicated instance, ALSA output
        self._vlc = vlc.Instance("--no-xlib", "--aout=alsa", "--file-caching=3000")
        self.player: vlc.MediaPlayer = None

        # Joystick via pygame (display not needed)
        pygame.init()
        pygame.joystick.init()
        if pygame.joystick.get_count() == 0:
            log.error("No joystick/controller found")
            sys.exit(1)
        self.joy = pygame.joystick.Joystick(0)
        self.joy.init()
        nbtn = self.joy.get_numbuttons()
        log.info(f"Controller: '{self.joy.get_name()}' | {nbtn} button(s)")

        # Per-button tracking arrays
        self._btn_prev = [0]   * nbtn   # last raw state
        self._btn_edge = [0.0] * nbtn   # timestamp of last state change
        self._btn_fire = [0.0] * nbtn   # timestamp of last fired action
        self._btn_down = [0.0] * nbtn   # timestamp of last DOWN event

        # Autosave thread
        threading.Thread(target=self._autosave_loop, daemon=True, name="autosave").start()

        # Restore last book + load it (paused)
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

    def _save_index(self):
        atomic_write(self._index_file(), str(self.idx))

    def _save_pos(self):
        if not self.player:
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

    def _reset_all_positions(self):
        count = 0
        for f in glob.glob(os.path.join(POS_DIR, "*.pos")):
            try:
                os.remove(f)
                count += 1
            except Exception as e:
                log.warning(f"Could not remove {f}: {e}")
        log.info(f"Reset {count} position file(s)")

    def _autosave_loop(self):
        while True:
            time.sleep(POSITION_SAVE_INTERVAL)
            if not self.paused:
                self._save_pos()

    # ── Book loading ──────────────────────────────────────────────────────────

    def _load_book(self, index: int, start_paused: bool):
        if self.player:
            try:
                self._save_pos()
                self.player.stop()
            except Exception:
                pass

        self.books = self._scan_books()
        if not self.books:
            log.error("No books found")
            return

        self.idx  = index % len(self.books)
        path      = self.books[self.idx]
        last_s    = self._load_pos(path)

        self.player = self._vlc.media_player_new()
        m = self._vlc.media_new(path)
        m.add_option(":input-fast-seek")
        m.add_option(":file-caching=3000")
        self.player.set_media(m)

        em = self.player.event_manager()
        em.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_end)

        self.player.play()

        # Wait until VLC is ready before seeking
        for _ in range(40):
            if self.player.get_state() in (vlc.State.Playing, vlc.State.Paused):
                break
            time.sleep(0.05)

        self.player.set_time(int(last_s * 1000))

        if start_paused:
            self.player.pause()
            self.paused = True
        else:
            self.paused = False

        self.ended = False

        try:
            from mutagen.mp3 import MP3
            self.length_s = MP3(path).info.length
        except Exception:
            raw = self.player.get_length()
            self.length_s = raw / 1000.0 if raw and raw > 0 else 0.0

        try:
            self.player.audio_set_volume(100)
        except Exception:
            pass

        self._save_index()
        log.info(f"Book: {Path(path).name} | pos={last_s:.1f}s | len={self.length_s:.1f}s")

    def _on_end(self, _event):
        try:
            self._save_pos()
        except Exception:
            pass
        self.ended  = True
        self.paused = False
        log.info(f"Finished: {Path(self.books[self.idx]).name}")

    # ── Playback actions ──────────────────────────────────────────────────────

    def _toggle_play(self):
        if not self.player:
            return
        if self.ended:
            self.player.set_time(0)
            self.player.play()
            self.paused = False
            self.ended  = False
            log.info("Restarting from beginning")
            return
        if self.paused:
            self.player.play()
            self.paused = False
            log.info("Play")
        else:
            self.player.pause()
            self.paused = True
            self._save_pos()
            log.info("Pause")

    def _seek(self, delta_s: int):
        if not self.player:
            return
        cur_ms = self.player.get_time() or 0
        if self.length_s > 0:
            max_ms = int(self.length_s * 1000)
        else:
            raw = self.player.get_length()
            max_ms = raw if raw and raw > 0 else 0
        tgt_ms = max(0, cur_ms + delta_s * 1000)
        if max_ms > 0:
            tgt_ms = min(tgt_ms, max(0, max_ms - 250))
        was_playing = (self.player.get_state() == vlc.State.Playing)
        self.player.set_time(tgt_ms)
        self._save_pos()
        if was_playing:
            self.player.play()
            self.paused = False
        else:
            self.player.pause()
            self.paused = True
        log.info(f"Seek {delta_s:+d}s → {tgt_ms / 1000.0:.1f}s")

    def _next_book(self):
        was_playing = not self.paused
        self._save_pos()
        self._load_book((self.idx + 1) % len(self.books), start_paused=not was_playing)
        log.info("Next book")

    def _reboot(self):
        log.info("Rebooting…")
        self._save_pos()
        rc = os.system("sudo /sbin/reboot")
        if rc != 0:
            log.error(f"Reboot failed (rc={rc}) — check sudoers")

    # ── Button polling (~100 Hz) ──────────────────────────────────────────────

    def _poll_buttons(self):
        now = time.time()
        pygame.event.pump()

        for btn in range(self.joy.get_numbuttons()):
            cur  = self.joy.get_button(btn)
            prev = self._btn_prev[btn]

            if cur == prev:
                continue

            # Edge detected — apply debounce
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
                    self._toggle_play();   self._btn_fire[btn] = now
                elif btn == BTN_FWD:
                    self._seek(+JUMP_SECONDS); self._btn_fire[btn] = now
                elif btn == BTN_BACK:
                    self._seek(-JUMP_SECONDS); self._btn_fire[btn] = now
                elif btn == BTN_NEXT:
                    self._next_book();     self._btn_fire[btn] = now
                # BTN_BLK: decision deferred to UP

            else:
                # ── UP ────────────────────────────────────────────────────────
                if btn == BTN_BLK:
                    if now - self._btn_fire[btn] < BTN_COOLDOWN:
                        continue
                    held = now - (self._btn_down[btn] or now)
                    if held >= BLACK_LONG_SEC:
                        self._reboot()
                    else:
                        self._reset_all_positions()
                    self._btn_fire[btn] = now

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        log.info("=== Audiobook player started ===")
        try:
            while True:
                self._poll_buttons()
                time.sleep(LOOP_SLEEP)
        except KeyboardInterrupt:
            log.info("Interrupted")
        finally:
            self._shutdown()

    def _shutdown(self):
        try:
            self._save_pos()
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
