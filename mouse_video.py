#!/usr/bin/env python3
"""
Sound-reactive mouse installation (video version).

A shy on-screen mouse lives in a hole in the wall. When the room is quiet for
long enough it creeps out to eat a piece of cheese (a paper cheese is hung in
front of the screen). As soon as it hears a sound it dashes back into the wall.

This version plays the animation as full-quality video clips through mpv, which
is a good fit for a Raspberry Pi 5 (hardware video decoding, plenty of RAM).
Sound level is read from a USB microphone via `arecord`, which is stable and
does not fight mpv over the audio device.

State machine
-------------
    SOUND        -> play v1 (empty scene) on a loop while sound is present
    COMING_OUT   -> after QUIET_SECONDS of quiet, play v2 once (mouse emerges);
                    when v2 finishes it flows into v3
    EATING       -> play v3 on a loop (mouse eats)
    RUNBACK      -> on any sound while out, play v4 once (mouse flees); v4 always
                    plays fully before the next state is chosen

The four clips sit next to this file:
    v1_quiet.mp4  v2_outtoeat.mp4  v3_eatingloop.mp4  v4_runback.mp4
"""

import json
import os
import socket
import subprocess
import time

import numpy as np

# --- Configuration -----------------------------------------------------------

# ALSA capture device for the USB microphone. Find yours with `arecord -l`;
# "plughw:1,0" means card 1, device 0. This can change when you move to new
# hardware, so check it on the Pi you actually run on.
CARD = "plughw:1,0"

# Microphone sample rate used for the level reading (Hz).
SAMPLERATE = 16000

# Loudness threshold. A reading above this counts as "sound".
# Lower = more sensitive. Tune it to sit just above the room's background noise.
THRESHOLD = 0.032

# Seconds of continuous quiet required before the mouse comes out.
QUIET_SECONDS = 20.0

# How many consecutive loud readings are needed to count as real sound.
# Debounces one-off noise spikes so the mouse doesn't twitch.
LOUD_HITS_NEEDED = 2

# Video files, relative to this script.
HOME = os.path.dirname(os.path.abspath(__file__))
V1 = os.path.join(HOME, "v1_quiet.mp4")        # empty scene, looped
V2 = os.path.join(HOME, "v2_outtoeat.mp4")     # mouse emerges, once
V3 = os.path.join(HOME, "v3_eatingloop.mp4")   # mouse eats, looped
V4 = os.path.join(HOME, "v4_runback.mp4")      # mouse flees, once

# Unix socket used to control mpv while it runs.
MPV_SOCKET = "/tmp/mpvsocket"


# --- Audio -------------------------------------------------------------------

def read_volume():
    """Record a one-second chunk from the mic and return its RMS level (0..1).

    Uses `arecord` in raw mode and computes the level in memory; nothing is
    written to disk. Returns 0.0 on any failure so the loop never crashes.
    """
    try:
        proc = subprocess.run(
            ["arecord", "-D", CARD, "-d", "1", "-f", "S16_LE",
             "-r", str(SAMPLERATE), "-c", "1", "-t", "raw", "-q"],
            capture_output=True, timeout=3,
        )
        if not proc.stdout:
            return 0.0
        samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32)
        if len(samples) == 0:
            return 0.0
        return float(np.sqrt(np.mean((samples / 32768.0) ** 2)))
    except Exception:
        return 0.0


# --- mpv control -------------------------------------------------------------

def start_mpv():
    """Launch mpv fullscreen, idle, controllable over an IPC socket."""
    if os.path.exists(MPV_SOCKET):
        os.remove(MPV_SOCKET)
    proc = subprocess.Popen([
        "mpv", "--fullscreen", "--ao=null", "--no-osc",
        "--no-input-default-bindings", "--input-ipc-server=" + MPV_SOCKET,
        "--idle=yes", "--force-window=yes", "--no-terminal",
        "--loop-file=inf", V1,
    ])
    # Wait for the control socket to appear.
    for _ in range(50):
        if os.path.exists(MPV_SOCKET):
            break
        time.sleep(0.1)
    time.sleep(0.5)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(MPV_SOCKET)
    sock.settimeout(0.05)
    return proc, sock


def mpv_command(sock, command):
    try:
        sock.send((json.dumps({"command": command}) + "\n").encode())
    except Exception:
        pass


def play(sock, path, loop):
    """Replace the current clip; loop it or play it once."""
    mpv_command(sock, ["loadfile", path, "replace"])
    mpv_command(sock, ["set_property", "loop-file", "inf" if loop else "no"])


def get_property(sock, name):
    """Ask mpv for a numeric property; return the value or None."""
    try:
        sock.send((json.dumps({"command": ["get_property", name]}) + "\n").encode())
        time.sleep(0.02)
        data = sock.recv(8192).decode()
        for line in data.splitlines():
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("error") == "success" and isinstance(msg.get("data"), (int, float)):
                return msg["data"]
    except Exception:
        pass
    return None


def near_end(sock):
    """True when the current non-looping clip is basically finished."""
    remaining = get_property(sock, "time-remaining")
    return remaining is not None and remaining <= 0.3


# --- Main --------------------------------------------------------------------

def main():
    for path in (V1, V2, V3, V4):
        if not os.path.exists(path):
            raise SystemExit(f"Missing video file: {path}")

    mpv_proc, sock = start_mpv()
    play(sock, V1, loop=True)

    state = "SOUND"
    last_sound = time.time()
    runback_started = 0.0
    loud_streak = 0

    print("Running. Ctrl+C to stop.")
    try:
        while True:
            vol = read_volume()          # blocks ~1s
            now = time.time()

            if vol > THRESHOLD:
                loud_streak += 1
            else:
                loud_streak = 0
            loud = loud_streak >= LOUD_HITS_NEEDED
            if loud:
                last_sound = now

            if state == "SOUND":
                if now - last_sound >= QUIET_SECONDS:
                    state = "COMING_OUT"
                    play(sock, V2, loop=False)
            elif state == "COMING_OUT":
                if near_end(sock):
                    play(sock, V3, loop=True)
                    state = "EATING"
                if loud:
                    state = "RUNBACK"
                    play(sock, V4, loop=False)
                    runback_started = now
            elif state == "EATING":
                if loud:
                    state = "RUNBACK"
                    play(sock, V4, loop=False)
                    runback_started = now
            elif state == "RUNBACK":
                if near_end(sock) or (now - runback_started >= 3.0):
                    if now - last_sound >= QUIET_SECONDS:
                        state = "COMING_OUT"
                        play(sock, V2, loop=False)
                    else:
                        state = "SOUND"
                        play(sock, V1, loop=True)

            print(f"vol={vol:.4f} state={state} "
                  f"quiet={now - last_sound:.0f}s   ", end="\r")
    except KeyboardInterrupt:
        pass
    finally:
        mpv_command(sock, ["quit"])
        mpv_proc.terminate()


if __name__ == "__main__":
    main()
