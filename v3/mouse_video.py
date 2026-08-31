#!/usr/bin/env python3
"""
Sound-reactive mouse installation (Raspberry Pi 5 / labwc).

Mice live in a hole in the wall. When the kitchen has been quiet for long
enough, one creeps out and settles into its loop. A sound sends it back in.
Every so often a dog turns up instead.

Everything about which photos play, in what order, at what speed, and when the
screen changes lives in scenes.json. This file is the engine. See SCENES.md.

    python3 build_scenes.py     # photos -> clips (run first, and after edits)
    python3 mouse_video.py      # run the installation

Sequences, following the design doc
-----------------------------------
    0 base      the wall, no mouse            4 easter egg   the dogs
    1 out       comes out                     5 loading      startup
    2 loop      stays, loops
    3 in        goes back in                  1R  rewind of 1

Normal order:  0 - 1 - 2 (loops until sound) - 3 - 0
Easter egg:    0 - 4 - 0        Startup:  0 - 5 - 0

What a sound does, by state
---------------------------
    0 base      nothing; the quiet timer just resets
    1 out       stop where it is and REWIND  ->  1 - 1R - 0
                (not a jump to 3: that would read as a teleport)
    2 loop      end the loop and go in       ->  2 - 3 - 0
                'finish_fast' plays out the rest of the current cycle at
                loop_exit_speed so the pose stays continuous; 'cut' jumps
                straight to 3. Set in timing.on_sound_during_loop.
    3 in        nothing; it is already leaving, at its normal speed
    4 easter    nothing at all, and the quiet timer is not reset
    5 loading   nothing

Notes on the hard-won bits (see TROUBLESHOOTING.md):
  * the mic is read on a background thread — reading it on the main loop
    stalls mpv and blanks the screen
  * mpv must use OpenGL, not its default Vulkan, or it displays nothing
  * mpv's IPC socket is drained by its own thread; letting replies and events
    pile up in that buffer eventually wedges mpv
"""

import collections
import json
import os
import random
import socket
import subprocess
import threading
import time

import numpy as np
import sounddevice as sd

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "scenes.json")
BUILD = os.path.join(HERE, "build")
LOGFILE = os.path.expanduser("~/mouse.log")
MPV_SOCKET = "/tmp/mpvsocket"


# --- Microphone ---------------------------------------------------------------
# Runs on sounddevice's own callback thread and only publishes numbers. The old
# version called sd.rec() in a loop, which opened and closed a stream for every
# block and so went deaf in the gaps between them — short claps fell through it.
# An InputStream is continuous, so nothing is missed.

class Mic:
    def __init__(self, cfg):
        self.cfg = cfg
        self.device = cfg.get("device", 0)
        self.samplerate = int(cfg.get("samplerate", 44100))
        self.block = int(float(cfg.get("block_seconds", 0.05)) * self.samplerate)

        self.auto = bool(cfg.get("auto_threshold", True))
        self.margin = float(cfg.get("noise_margin", 3.0))
        self.fixed_threshold = float(cfg.get("threshold", 0.04))
        self.min_threshold = float(cfg.get("min_threshold", 0.015))

        self.hits_needed = int(cfg.get("flee_hits_needed", 3))
        self.hit_window = float(cfg.get("flee_hit_window", 0.35))

        # Measure only the speech band. A kitchen's resting level is mostly
        # low-frequency rumble — fridge, extractor, the building — while a voice
        # from across the room lives around 300-3000Hz. Ignoring the rumble
        # lifts a distant voice clear of a floor it otherwise sits inside.
        band = cfg.get("band_hz", [300, 3000])
        self.band = tuple(band) if band else None
        self._mask = None
        self._weight = None

        self.lock = threading.Lock()
        self.level = 0.0
        self.level_raw = 0.0
        self.status = "starting"
        self.recent = collections.deque(maxlen=200)     # (time, rms), ~10s
        self.floor_samples = collections.deque(maxlen=600)  # ~30s for the noise floor
        self._stream = None

    def _band_rms(self, x):
        """RMS of just the speech band, on the same scale as a plain RMS."""
        n = len(x)
        if self._mask is None or len(self._mask) != n // 2 + 1:
            freqs = np.fft.rfftfreq(n, 1.0 / self.samplerate)
            self._mask = (freqs >= self.band[0]) & (freqs <= self.band[1])
            # one-sided spectrum: every bin but DC (and Nyquist) counts twice
            w = np.full(freqs.shape, 2.0)
            w[0] = 1.0
            if n % 2 == 0:
                w[-1] = 1.0
            self._weight = w
        spec = np.fft.rfft(x)
        power = np.sum(self._weight[self._mask] * np.abs(spec[self._mask]) ** 2)
        return float(np.sqrt(power)) / n

    def _callback(self, indata, frames, time_info, status):
        x = np.asarray(indata, dtype=np.float64).reshape(-1)
        raw = float(np.sqrt(np.mean(x * x)))
        rms = self._band_rms(x) if self.band else raw
        now = time.time()
        with self.lock:
            self.level = rms
            self.level_raw = raw
            self.recent.append((now, rms))
            self.floor_samples.append(rms)
            self.status = "ok"

    def start(self):
        try:
            self._stream = sd.InputStream(
                device=self.device, channels=1, samplerate=self.samplerate,
                blocksize=self.block, callback=self._callback)
            self._stream.start()
        except Exception as e:
            self.status = f"error: {e}"

    def threshold(self):
        """Where 'sound' begins. Auto mode tracks the room's own noise floor, so
        a kitchen with a fridge hum doesn't keep the mouse hidden forever."""
        if not self.auto:
            return self.fixed_threshold
        with self.lock:
            samples = list(self.floor_samples)
        if len(samples) < 40:
            return self.fixed_threshold
        floor = float(np.percentile(samples, 20))
        return max(self.min_threshold, floor * self.margin)

    def is_loud(self, threshold):
        """True only when several *distinct* mic blocks in a short window were
        loud. The old code counted main-loop iterations, which spin faster than
        the mic produces samples, so it was really counting the same sample
        three times — the debounce did nothing."""
        cutoff = time.time() - self.hit_window
        with self.lock:
            hits = sum(1 for t, rms in self.recent if t >= cutoff and rms > threshold)
        return hits >= self.hits_needed

    def snapshot(self):
        with self.lock:
            return self.level, self.status


# --- mpv ----------------------------------------------------------------------

class Mpv:
    """mpv wrapper with a reader thread.

    The prototype wrote commands to the socket but only ever read from it inside
    get_property(). mpv also pushes events down that socket, so the unread bytes
    accumulated until the buffer filled and mpv wedged. It also meant
    get_property() could return a *stale* reply — which is how the emerge clip
    sometimes got skipped, because 'time-remaining' still described the clip
    that had just been replaced.

    Here a thread drains the socket continuously and keeps the properties we
    care about in a dict, tagged with the file they belong to. The main loop
    only reads that dict, so it never blocks and never reads a stale value.
    """

    OBSERVED = ["time-pos", "duration", "eof-reached", "path", "pause"]

    def __init__(self):
        self.proc = None
        self.sock = None
        self.lock = threading.Lock()
        self.props = {}
        self.requested = None       # the file we last asked mpv to play
        self._stop = False

    # -- lifecycle --
    def start(self, first_file):
        try:
            if os.path.exists(MPV_SOCKET):
                os.remove(MPV_SOCKET)
        except OSError:
            pass

        # --vo=gpu --gpu-api=opengl is ESSENTIAL on the Pi 5. mpv's default
        # Vulkan backend fails with VK_ERROR_OUT_OF_HOST_MEMORY, then retries
        # forever: the process stays alive but shows nothing and ignores the
        # socket. --keep-open holds the last frame of a one-shot clip instead
        # of cutting to black (that is how a no-idle character "holds").
        self.proc = subprocess.Popen([
            "mpv", "--fullscreen", "--vo=gpu", "--gpu-api=opengl", "--no-osc",
            "--no-input-default-bindings", "--input-ipc-server=" + MPV_SOCKET,
            "--idle=yes", "--force-window=yes", "--no-terminal",
            "--keep-open=yes", "--really-quiet",
            "--audio=no", "--cache=no",
            "--demuxer-lavf-probesize=32768", "--demuxer-lavf-analyzeduration=0",
            "--loop-file=inf", first_file])

        for _ in range(100):
            if os.path.exists(MPV_SOCKET):
                break
            time.sleep(0.1)
        for _ in range(50):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(MPV_SOCKET)
                s.settimeout(0.5)
                self.sock = s
                break
            except Exception:
                time.sleep(0.2)
        if self.sock is None:
            return False

        self.requested = first_file
        self._stop = False
        threading.Thread(target=self._reader, daemon=True).start()
        for i, name in enumerate(self.OBSERVED):
            self.command(["observe_property", i + 1, name])
        return True

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def quit(self):
        self._stop = True
        self.command(["quit"])
        if self.proc:
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.terminate()

    # -- io --
    def _reader(self):
        buf = b""
        while not self._stop:
            try:
                chunk = self.sock.recv(65536)
                if not chunk:
                    time.sleep(0.1)
                    continue
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    if msg.get("event") == "property-change":
                        with self.lock:
                            self.props[msg.get("name")] = msg.get("data")
            except socket.timeout:
                continue
            except Exception:
                time.sleep(0.2)

    def command(self, cmd):
        try:
            self.sock.send((json.dumps({"command": cmd}) + "\n").encode())
        except Exception:
            pass

    # -- playback --
    def play(self, path, loop, speed=1.0):
        with self.lock:
            self.requested = path
            # Forget the old clip's numbers so progress() can't answer with them.
            self.props["time-pos"] = None
            self.props["duration"] = None
            self.props["eof-reached"] = None
            self.props["path"] = None
        self.command(["set_property", "speed", float(speed)])
        self.command(["loadfile", path, "replace"])
        self.command(["set_property", "loop-file", "inf" if loop else "no"])
        # --keep-open=yes pauses mpv at the end of a one-shot clip, and "pause"
        # is a GLOBAL property that survives the next loadfile. Without this,
        # everything after the first one-shot clip loads frozen on frame 1.
        self.command(["set_property", "pause", False])
        self.command(["set_property", "ab-loop-a", "no"])
        self.command(["set_property", "ab-loop-b", "no"])

    def set_speed(self, speed):
        self.command(["set_property", "speed", float(speed)])

    def seek(self, seconds):
        self.command(["seek", float(seconds), "absolute"])

    def ab_loop(self, a, b):
        """Loop a section of the current file. This is how the mouse stays in
        its loop without mpv loading anything — playback simply repeats."""
        self.command(["set_property", "ab-loop-a", float(a)])
        self.command(["set_property", "ab-loop-b", float(b)])

    def clear_ab_loop(self):
        """Release the loop; playback runs on into whatever follows it in the
        file — which is the in sequence. No load, no seek, no gap."""
        self.command(["set_property", "ab-loop-a", "no"])
        self.command(["set_property", "ab-loop-b", "no"])

    def _on_requested_file(self):
        p = self.props.get("path")
        return p is not None and self.requested is not None and \
            os.path.basename(p) == os.path.basename(self.requested)

    def progress(self):
        """(position, duration) for the clip we asked for, or (None, None)."""
        with self.lock:
            if not self._on_requested_file():
                return None, None
            pos, dur = self.props.get("time-pos"), self.props.get("duration")
        if isinstance(pos, (int, float)) and isinstance(dur, (int, float)) and dur > 0:
            return float(pos), float(dur)
        return None, None

    def paused(self):
        with self.lock:
            return self.props.get("pause") is True

    def finished(self):
        with self.lock:
            if not self._on_requested_file():
                return False
            if self.props.get("eof-reached") is True:
                return True
        pos, dur = self.progress()
        return pos is not None and dur - pos <= 0.08

    def near_loop_end(self, margin=0.25):
        pos, dur = self.progress()
        return pos is not None and dur - pos <= margin


# --- Scenes -------------------------------------------------------------------

def clip(name):
    return os.path.join(BUILD, name + ".mp4")


class Character:
    def __init__(self, spec, defaults, seg):
        self.name = spec["name"]
        self.weight = float(spec.get("weight", 1))
        self.quiet_seconds = float(spec.get("quiet_seconds") or defaults["quiet_seconds"])
        self.rewind_speed = float(spec.get("rewind_speed") or defaults["rewind_speed"])

        # out + loop + in live in one continuous file; these are the seconds at
        # which one becomes the next.
        self.main = clip(f"{self.name}__main")
        self.rewind = clip(f"{self.name}__rewind")
        self.out_end = float(seg.get("out_end", 0.0))
        self.loop_end = float(seg.get("loop_end", 0.0))
        self.total = float(seg.get("total", 0.0))
        self.has_in = bool(seg.get("has_in", False))
        # Loop points come from the encoded file's real frame timestamps
        # (build_scenes.py reads them with ffprobe), not from frames/fps
        # arithmetic — being one frame out here puts the last frame of the out
        # sequence at the head of every loop cycle.
        fps_out = float(seg.get("fps_out", 0) or 30)
        frame = 1.0 / fps_out
        a = seg.get("loop_a")
        b = seg.get("loop_b")
        if a is None or b is None:                 # stale segments.json
            a, b = self.out_end, self.loop_end
        # A quarter-frame past the first loop frame's own timestamp: far enough
        # that an exact seek cannot resolve back onto the frame before it, and
        # far short of the next frame.
        self.loop_a = a + frame * 0.25
        self.loop_b = max(self.loop_a + frame, b - frame * 0.25)

    def missing(self):
        out = [p for p in (self.main, self.rewind) if not os.path.exists(p)]
        if self.total <= 0:
            out.append(f"{self.name}: no segment boundaries — re-run build_scenes.py")
        return out


class EasterEggs:
    """The dogs. They take the slot a mouse would have used, ignore sound
    entirely, and alternate so both get seen."""

    def __init__(self, spec):
        spec = spec or {}
        # speed is a runtime multiplier: 0.7 plays the dog at 70% — slower and
        # more of an amble. It needs no rebuild, unlike changing fps.
        self.seqs = [(e["name"], clip("easter_" + e["name"]), float(e.get("speed", 1.0)))
                     for e in (spec.get("sequences") or []) if e.get("name")]
        every = spec.get("every", [4, 8])
        self.lo, self.hi = (every if isinstance(every, list) else [every, every])
        self.order = spec.get("order", "alternate")
        self.post_quiet = float(spec.get("post_quiet_seconds", 8.0))
        self.index = 0
        self.countdown = self._roll()

    def _roll(self):
        return random.randint(int(self.lo), int(self.hi)) if self.seqs else 10 ** 9

    def missing(self):
        return [p for _, p, _ in self.seqs if not os.path.exists(p)]

    def due(self):
        return bool(self.seqs) and self.countdown <= 0

    def take(self):
        """Return the next egg's (name, clip, speed) and re-arm the counter."""
        if self.order == "random":
            egg = random.choice(self.seqs)
        else:
            egg = self.seqs[self.index % len(self.seqs)]
            self.index += 1
        self.countdown = self._roll()
        return egg

    def tick(self):
        self.countdown -= 1


def pick_character(characters, avoid=None):
    """Random, but never the same character twice running — with a small cast,
    pure random repeats often enough to read as a bug."""
    options = [c for c in characters if c is not avoid] or characters
    return random.choices(options, weights=[c.weight for c in options], k=1)[0]


pending_seek = [None]


# --- Main ---------------------------------------------------------------------

def main():
    with open(CONFIG) as f:
        cfg = json.load(f)

    timing = cfg.get("timing", {})
    defaults = {
        "quiet_seconds": float(timing.get("quiet_seconds", 20.0)),
        "rewind_speed": float(timing.get("rewind_speed", 1.5)),
    }
    cooldown_seconds = float(timing.get("cooldown_seconds", 6.0))
    loop_exit_mode = timing.get("on_sound_during_loop", "finish_fast")
    loop_exit_speed = float(timing.get("loop_exit_speed", 3.0))

    base = clip("base")
    loading = clip("loading") if cfg.get("loading") else None
    eggs = EasterEggs(cfg.get("easter_eggs"))
    seg_path = os.path.join(BUILD, "segments.json")
    try:
        with open(seg_path) as f:
            segs = json.load(f)
    except Exception:
        segs = {}
    characters = [Character(c, defaults, segs.get(c.get("name"), {}))
                  for c in cfg.get("characters", [])]

    missing = ([] if os.path.exists(base) else [base]) + eggs.missing()
    for c in characters:
        missing += c.missing()
    if loading and not os.path.exists(loading):
        missing.append(loading)
    if missing or not characters:
        print("Missing clips — run:  python3 build_scenes.py\n")
        for m in missing:
            print(f"  - {m}")
        if not characters:
            print("  - scenes.json defines no characters")
        raise SystemExit(1)

    mic = Mic(cfg.get("audio", {}))
    mic.start()

    mpv = Mpv()
    if not mpv.start(base):
        raise SystemExit("Could not connect to mpv's IPC socket")

    character = pick_character(characters)
    current = None            # the clip currently on screen
    last_sound = time.time()
    state_entered = time.time()
    next_quiet = character.quiet_seconds
    last_log = 0.0

    # 0 - 5 - 0 : the loading sequence plays once, at startup.
    if loading:
        mpv.play(loading, loop=False)
        state = "LOADING"
    else:
        state = "BASE"

    def enter(new_state):
        nonlocal state, state_entered
        state, state_entered = new_state, time.time()

    def go_base():
        mpv.play(base, loop=True)
        enter("COOLDOWN")

    try:
        while True:
            now = time.time()

            if not mpv.alive():
                mpv.start(base)
                go_base()
                continue

            if mpv.paused():
                mpv.command(["set_property", "pause", False])

            threshold = mic.threshold()
            loud = mic.is_loud(threshold)
            # Sound during an easter egg is ignored outright — including for the
            # quiet timer, so a room laughing at the dog doesn't then have to
            # fall silent all over again before the next mouse.
            if loud and state not in ("EASTER", "LOADING"):
                last_sound = now
            quiet_for = now - last_sound

            # ----- 5 loading: 0 - 5 - 0 -----
            if state == "LOADING":
                if mpv.finished() or now - state_entered > 60:
                    go_base()

            # ----- 0 base: waiting for the room to settle -----
            elif state == "BASE":
                if quiet_for >= next_quiet:
                    if eggs.due():
                        name, path, speed = eggs.take()
                        mpv.play(path, loop=False, speed=speed)
                        enter("EASTER")
                    else:
                        eggs.tick()
                        mpv.play(character.main, loop=False)
                        enter("OUT")

            # ----- 1 out: interruptible, and rewinds from where it got to -----
            elif state == "OUT":
                pos, _ = mpv.progress()
                if loud:
                    # 1 - 1R - 0. Mirror the position so the mouse turns around
                    # where it stands. This is the one transition that still
                    # loads a file: the rewind runs the other way.
                    mpv.play(character.rewind, loop=False, speed=character.rewind_speed)
                    if pos is not None:
                        pending_seek[0] = max(0.0, character.out_end - pos)
                    enter("REWIND")
                elif pos is not None and pos >= character.out_end:
                    # Straight into the loop — same file, so nothing loads.
                    mpv.ab_loop(character.loop_a, character.loop_b)
                    enter("LOOP")

            # ----- 1R rewind: always plays out; sound is ignored -----
            elif state == "REWIND":
                if mpv.finished() or now - state_entered > 20:
                    go_base()

            # ----- 2 loop: the common case. Sound -> 3 -----
            elif state == "LOOP":
                if loud:
                    # Release the loop and the file simply carries on into the
                    # in sequence. 'finish_fast' races through what is left of
                    # the current cycle first; 'cut' jumps to the boundary.
                    mpv.clear_ab_loop()
                    if loop_exit_mode == "cut":
                        mpv.seek(character.loop_end)
                        enter("IN")
                    else:
                        mpv.set_speed(loop_exit_speed)
                        enter("LOOP_EXIT")

            elif state == "LOOP_EXIT":
                pos, _ = mpv.progress()
                if (pos is not None and pos >= character.loop_end) or now - state_entered > 5.0:
                    mpv.set_speed(1.0)
                    enter("IN")
                elif mpv.finished():
                    go_base()

            # ----- 3 in: runs to the end of the file, at its normal speed -----
            elif state == "IN":
                if mpv.finished() or now - state_entered > 20:
                    go_base()

            # ----- 4 easter egg: 0 - 4 - 0, deaf to the room -----
            elif state == "EASTER":
                if mpv.finished() or now - state_entered > 60:
                    mpv.play(base, loop=True)
                    last_sound = now              # start the wait from here
                    next_quiet = eggs.post_quiet  # a shorter wait after a dog
                    enter("BASE")

            # ----- back to the wall, and nobody may come out yet -----
            elif state == "COOLDOWN":
                if now - state_entered >= cooldown_seconds:
                    character = pick_character(characters, avoid=character)
                    next_quiet = character.quiet_seconds
                    enter("BASE")

            # The seek for 1R can only be sent once mpv has the clip loaded.
            if pending_seek[0] is not None:
                pos, dur = mpv.progress()
                if pos is not None:
                    mpv.seek(min(pending_seek[0], max(0.0, dur - 0.05)))
                    pending_seek[0] = None
                elif now - state_entered > 1.0:
                    pending_seek[0] = None

            if now - last_log > 0.5:
                level, mic_status = mic.snapshot()
                try:
                    with open(LOGFILE, "w") as f:
                        f.write(
                            f"vol={level:.4f} thr={threshold:.4f} mic={mic_status}\n"
                            f"state={state} character={character.name} "
                            f"eggs_in={eggs.countdown}\n"
                            f"quiet={quiet_for:.0f}s / needs {next_quiet:.0f}s "
                            f"in_state={now - state_entered:.0f}s\n")
                except Exception:
                    pass
                last_log = now

            time.sleep(0.03)

    except KeyboardInterrupt:
        pass
    finally:
        mpv.quit()


if __name__ == "__main__":
    main()
