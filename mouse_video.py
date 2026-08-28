#!/usr/bin/env python3
"""
Sound-reactive mouse installation (video version, Raspberry Pi 5 / labwc).

A shy on-screen mouse lives in a hole in the wall. When the room is quiet for
long enough it creeps out to eat a piece of cheese (a paper cheese is hung in
front of the screen). As soon as it hears a sound it dashes back into the wall.

The animation plays as full-quality video clips through mpv. Sound level is read
from a USB microphone with sounddevice, in a background thread so it never
blocks playback. See README.md and TROUBLESHOOTING.md for the full story of the
problems we hit and how each was solved.

State machine
-------------
    SOUND        -> play v1 (empty scene) on a loop while sound is present
    COMING_OUT   -> after QUIET_SECONDS of quiet, play v2 once (mouse emerges);
                    when v2 finishes it flows into v3
    EATING       -> play v3 on a loop (mouse eats)
    RUNBACK      -> on any sound while out, play v4 once (mouse flees); v4 always
                    plays fully before the next state is chosen
"""

import json
import os
import socket
import subprocess
import threading
import time

import numpy as np
import sounddevice as sd

# --- Configuration -----------------------------------------------------------

# sounddevice index of the USB microphone. Confirm with:
#   python3 -c "import sounddevice as sd; print(sd.query_devices())"
# NOTE: this index is NOT the same as the ALSA card number, and it can change
# between machines / OS images. On our Pi 5 (Trixie) it was 0.
DEVICE = 0

SAMPLERATE = 44100
BLOCK = 0.05           # seconds of audio read per measurement (small = responsive)

# Loudness that counts as "sound". Set it just above the room's background level.
# Quiet home ~0.01; a noisier room (e.g. a gallery/classroom) may need 0.04+.
THRESHOLD = 0.04

QUIET_SECONDS = 20.0   # continuous quiet needed before the mouse comes out
LOUD_HITS_NEEDED = 3   # consecutive loud reads that count as real sound (debounce)

LOGFILE = os.path.expanduser("~/mouse.log")

HOME = os.path.dirname(os.path.abspath(__file__))
V1 = os.path.join(HOME, "v1_quiet.mp4")        # empty scene, looped
V2 = os.path.join(HOME, "v2_outtoeat.mp4")     # mouse emerges, once
V3 = os.path.join(HOME, "v3_eatingloop.mp4")   # mouse eats, looped
V4 = os.path.join(HOME, "v4_runback.mp4")      # mouse flees, once
MPV_SOCKET = "/tmp/mpvsocket"


# --- Microphone (background thread) ------------------------------------------
# Reading the mic must NOT happen on the main loop: a blocking read stalls the
# loop and interferes with mpv's display (this caused a black screen). So it
# runs here and just publishes the latest level into vol_now.

vol_now = 0.0
mic_status = "starting"

def audio_loop():
    global vol_now, mic_status
    n = int(BLOCK * SAMPLERATE)
    while True:
        try:
            audio = sd.rec(n, samplerate=SAMPLERATE, channels=1,
                           device=DEVICE, blocking=True)
            vol_now = float(np.sqrt(np.mean(audio ** 2)))
            mic_status = "ok"
        except Exception as e:
            vol_now = 0.0
            mic_status = f"error: {e}"
            time.sleep(0.2)


# --- mpv control -------------------------------------------------------------

def start_mpv():
    if os.path.exists(MPV_SOCKET):
        os.remove(MPV_SOCKET)
    # --vo=gpu --gpu-api=opengl is ESSENTIAL on the Pi 5: the default Vulkan
    # backend failed with VK_ERROR_OUT_OF_HOST_MEMORY and never displayed
    # anything (the process stayed alive but frozen). OpenGL works.
    proc = subprocess.Popen([
        "mpv", "--fullscreen", "--vo=gpu", "--gpu-api=opengl", "--no-osc",
        "--no-input-default-bindings", "--input-ipc-server=" + MPV_SOCKET,
        "--idle=yes", "--force-window=yes", "--no-terminal",
        "--loop-file=inf", V1])
    # Wait for the socket file, then retry connecting until mpv is ready. The
    # retry loop matters at boot, when mpv may not be listening immediately.
    for _ in range(100):
        if os.path.exists(MPV_SOCKET):
            break
        time.sleep(0.1)
    sock = None
    for _ in range(50):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(MPV_SOCKET)
            s.settimeout(0.05)
            sock = s
            break
        except Exception:
            time.sleep(0.2)
    return proc, sock


def mpv_command(sock, command):
    try:
        sock.send((json.dumps({"command": command}) + "\n").encode())
    except Exception:
        pass


def play(sock, path, loop):
    mpv_command(sock, ["loadfile", path, "replace"])
    mpv_command(sock, ["set_property", "loop-file", "inf" if loop else "no"])


def get_property(sock, name):
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
    remaining = get_property(sock, "time-remaining")
    return remaining is not None and remaining <= 0.3


# --- Main --------------------------------------------------------------------

def main():
    for path in (V1, V2, V3, V4):
        if not os.path.exists(path):
            raise SystemExit(f"Missing video file: {path}")

    threading.Thread(target=audio_loop, daemon=True).start()

    mpv_proc, sock = start_mpv()
    if sock is None:
        raise SystemExit("Could not connect to mpv socket")
    play(sock, V1, loop=True)

    state = "SOUND"
    last_sound = time.time()
    runback_started = 0.0
    loud_streak = 0
    last_log = 0.0

    try:
        while True:
            now = time.time()
            if vol_now > THRESHOLD:
                loud_streak += 1
            else:
                loud_streak = 0
            loud = loud_streak >= LOUD_HITS_NEEDED
            if loud:
                last_sound = now

            if state == "SOUND":
                if now - last_sound >= QUIET_SECONDS:
                    state = "COMING_OUT"; play(sock, V2, loop=False)
            elif state == "COMING_OUT":
                if near_end(sock):
                    play(sock, V3, loop=True); state = "EATING"
                if loud:
                    state = "RUNBACK"; play(sock, V4, loop=False); runback_started = now
            elif state == "EATING":
                if loud:
                    state = "RUNBACK"; play(sock, V4, loop=False); runback_started = now
            elif state == "RUNBACK":
                if near_end(sock) or (now - runback_started >= 3.0):
                    if now - last_sound >= QUIET_SECONDS:
                        state = "COMING_OUT"; play(sock, V2, loop=False)
                    else:
                        state = "SOUND"; play(sock, V1, loop=True)

            # Write status to a log file so it can be inspected while the
            # program runs unattended (e.g. under autostart): cat ~/mouse.log
            if now - last_log > 0.5:
                try:
                    with open(LOGFILE, "w") as f:
                        f.write(f"vol={vol_now:.4f} mic={mic_status} "
                                f"state={state} quiet={now - last_sound:.0f}s "
                                f"threshold={THRESHOLD}\n")
                except Exception:
                    pass
                last_log = now
            time.sleep(0.03)
    except KeyboardInterrupt:
        pass
    finally:
        mpv_command(sock, ["quit"])
        mpv_proc.terminate()


if __name__ == "__main__":
    main()
