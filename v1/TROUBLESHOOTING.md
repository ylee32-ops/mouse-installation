# Troubleshooting Log

Everything that went wrong while building this, and how each was solved. If the
installation misbehaves, start here — most symptoms below have a known cause.

The single most important fix is #5 (mpv Vulkan → OpenGL). If the program runs
but nothing shows on screen, that's almost certainly it.

---

## 1. Black screen a few seconds into playback

**Symptom.** Video played, then the screen went black after a few seconds. It
came back whenever a sound was made.

**Cause.** The microphone was read on the main loop with a call that blocks for
about a second each time. That stall interfered with mpv's display and blanked
the screen.

**Fix.** Read the microphone in a **background (daemon) thread** that only
publishes the latest level into a shared variable. The main loop never blocks,
and mpv keeps displaying. This is why `audio_loop()` runs on its own thread.

---

## 2. Sound detection not sensitive enough

**Symptom.** After moving the mic off the main loop, short sounds (a single
clap) often didn't register.

**Cause.** We were recording a full 1-second chunk and averaging it, so a brief
sound was diluted across the second.

**Fix.** Switched to **sounddevice** reading small `0.05 s` blocks continuously.
Short sounds now show up immediately.

---

## 3. Microphone not found / wrong device index

**Symptom.** sounddevice couldn't open the mic, or read silence.

**Cause.** The **sounddevice index is not the ALSA card number**, and it changes
between machines and OS images. On the Pi Zero 2 W it was `1`; after moving to
the Pi 5 with the newer OS it was `0` — while ALSA still called the mic
`hw:2,0`.

**Fix.** Always confirm the index on the actual machine:

```bash
python3 -c "import sounddevice as sd; print(sd.query_devices())"
```

Set `DEVICE` at the top of `mouse_video.py` to the number in front of the
`USB PnP Sound Device ... (N in, ...)` line.

---

## 4. Mouse never comes out

**Symptom.** The mouse stays hidden. In the log, `quiet` keeps resetting to `0`
and `state` stays `SOUND`.

**Cause.** Background noise in the room is above `THRESHOLD`, so the program
thinks there is always sound. Our threshold of `0.02` worked at home but the
classroom's ambient level sat around `0.022`, constantly retriggering.

**Fix.** Raise `THRESHOLD`. We use `0.04`. Check the room's quiet level in
`~/mouse.log` (`vol=...`) and set the threshold just above it. Too high and the
mouse ignores real sounds; too low and it never comes out.

---

## 5. Autostart runs, but the screen is frozen and the mouse never moves

**This was the big one — and the cause was not obvious.**

**Symptom.** On boot the program was clearly running (`pgrep -f mouse_video.py`
returned a PID) and `~/mouse.log` showed correct state changes (e.g.
`state=EATING`). But the screen stayed on the first frame, and mpv ignored every
command sent over its socket. Running the exact same program **manually** worked
fine.

**How we found it.** Running mpv directly in the terminal printed the real
error, which `--no-terminal` had been hiding:

```
[vo/gpu/libplacebo] vk->CreateSwapchainKHR(...): VK_ERROR_OUT_OF_HOST_MEMORY
[vo/gpu/libplacebo] Failed (re)creating swapchain!
```

**Cause.** mpv's default GPU backend on the Pi 5 is **Vulkan**, and creating the
Vulkan swapchain failed with out-of-memory. mpv then looped forever retrying, so
the process was alive but displayed nothing and stopped responding to socket
commands. The state machine kept running (hence the log looked healthy), but its
`loadfile` commands went nowhere.

**Fix.** Force mpv to use **OpenGL** instead of Vulkan by adding to the mpv
launch arguments:

```
--vo=gpu --gpu-api=opengl
```

With that, mpv displays correctly and responds to commands. This is in
`start_mpv()`.

**Debugging tip.** When mpv seems frozen, run one clip directly and read the
output:

```bash
XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 \
  mpv --vo=gpu --gpu-api=opengl --fullscreen --loop-file=inf ~/v3_eatingloop.mp4
```

---

## 6. Wayland vs X11 environment (labwc)

**Background.** Current Raspberry Pi OS uses the **labwc** Wayland compositor,
not X11. That affects how you point mpv at the screen.

- **Manual run over SSH:** the SSH session has no display set, so provide it:
  `XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 python3 mouse_video.py`.
  The socket name (`wayland-0`) can be confirmed with
  `ls /run/user/$(id -u)/wayland-*`. `DISPLAY=:0` also works via Xwayland.
- **Autostart:** labwc's `~/.config/labwc/autostart` is run after the desktop is
  up and already provides `WAYLAND_DISPLAY`, so the program finds the screen
  without extra setup. Our wrapper still exports the variables to be safe.

**Gotcha.** labwc will not run `autostart` unless that file is **executable**:

```bash
chmod +x ~/.config/labwc/autostart
```

---

## 7. Screen goes blank when idle (screen blanking)

**Note.** We suspected screen blanking early on, but it turned out the "black
screen" was #1 (mic blocking), confirmed because a single looping mpv never
blanked. If you *do* see true idle blanking on labwc, it is controlled by
`swayidle` in `~/.config/labwc/autostart`; a single always-updating video
usually keeps the screen awake anyway.

---

## 8. Fullscreen offset to the bottom-right after a power-cut reboot

**Symptom.** Once, after pulling power, the video wasn't centered/fullscreen —
it sat toward the bottom-right.

**Status.** The screen resolution was still correct (1920x1080), so this was an
mpv/HDMI timing hiccup at boot, not a resolution change. A clean reboot restored
it. If it recurs often, add explicit geometry to mpv (e.g. `--geometry=100%x100%+0+0`)
and consider avoiding hard power-cuts.

---

## Quick diagnosis checklist

```bash
pgrep -f mouse_video.py        # is the program running?
pgrep -f mpv | wc -l           # exactly one mpv should be running
cat ~/mouse.log                # vol / mic / state / quiet — read it twice to see it change
ls -l /tmp/mpvsocket           # mpv control socket should exist
```

- Program running, log updates, but screen frozen → **#5 (use OpenGL)**.
- `mic=error` in the log → **#3 (wrong DEVICE index)**.
- `quiet` never climbs, mouse hidden → **#4 (raise THRESHOLD)**.
- Black screen only when the mic reads → **#1 (mic must be on a thread)**.
