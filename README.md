# Sound-Reactive Mouse Installation

A screen-based interactive installation. A shy on-screen mouse lives in a hole
in the wall. When the room is quiet for long enough, it creeps out to nibble a
piece of cheese (a paper cheese is hung in front of the screen). The moment it
hears a sound — a clap, a spoken word — it dashes back into the wall.

The piece is a small state machine that swaps between short stop-motion clips
filmed with plushies, driven by the room's sound level.

> New to the project? Read this file for how it works and how to run it, then
> read `TROUBLESHOOTING.md` for the problems we hit while building it and how
> each one was solved — that document will save you a lot of time.

## How it works

The animation plays as full-quality video clips through **mpv**. Sound level is
read from a USB microphone with **sounddevice**, in a **background thread** so
reading the mic never blocks video playback. The microphone reads the current
sound *level* only — no audio is recorded or saved.

### State machine

| State        | Plays                     | Leaves when                                 |
|--------------|---------------------------|---------------------------------------------|
| `SOUND`      | `v1_quiet.mp4` (loop)     | quiet for `QUIET_SECONDS` -> `COMING_OUT`   |
| `COMING_OUT` | `v2_outtoeat.mp4` (once)  | clip ends -> `EATING`; sound -> `RUNBACK`   |
| `EATING`     | `v3_eatingloop.mp4` (loop)| sound -> `RUNBACK`                          |
| `RUNBACK`    | `v4_runback.mp4` (once)   | clip ends -> `COMING_OUT` or `SOUND`        |

`v4_runback.mp4` always plays to the end before the next state is chosen, so the
flee never gets cut off mid-scramble.

## Hardware

- Raspberry Pi 5 (has hardware video decode and enough RAM for smooth video)
- HDMI screen (we used an Acer KB242Y, 1920x1080)
- USB microphone / USB audio input
- Official 5V power supply

The project was originally prototyped on a Pi Zero 2 W, which could not play
video reliably (no usable hardware video decode, only 512 MB RAM). The Pi 5
handles the video version comfortably.

## Software setup

On Raspberry Pi OS (Bookworm or Trixie, which use the **labwc** Wayland desktop):

```bash
sudo apt update
sudo apt install mpv libportaudio2 -y
pip install sounddevice numpy --break-system-packages
```

Find the microphone's sounddevice index:

```bash
python3 -c "import sounddevice as sd; print(sd.query_devices())"
```

Look for the `USB PnP Sound Device` line with `in` channels, and note the number
in front of it. Set `DEVICE` at the top of `mouse_video.py` to match. **This
index is not the ALSA card number and can change between machines** — on our
Pi 5 it was `0`, even though ALSA listed the mic as `hw:2,0`.

## The video files

Put the four clips next to `mouse_video.py`:

```
v1_quiet.mp4        empty scene (no mouse)
v2_outtoeat.mp4     mouse emerges toward the cheese
v3_eatingloop.mp4   mouse eating (loops seamlessly)
v4_runback.mp4      mouse flees back into the wall
```

These are large, so they are **not** stored in the repo (see `.gitignore`).
Share them separately (drive, USB stick) and copy them onto the Pi alongside the
script, e.g. from your computer:

```bash
rsync -avz --progress ~/Downloads/v1_quiet.mp4 ~/Downloads/v2_outtoeat.mp4 \
  ~/Downloads/v3_eatingloop.mp4 ~/Downloads/v4_runback.mp4 \
  sophie@raspberrypi.local:/home/sophie/
```

## Running it manually (for testing)

Over SSH you must tell mpv where the screen is. On the labwc (Wayland) desktop:

```bash
XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 python3 mouse_video.py
```

(`1000` is the user id; `wayland-0` is the Wayland socket — confirm with
`ls /run/user/$(id -u)/wayland-*`.) The older `DISPLAY=:0` also works via
Xwayland, but Wayland is the native path on this OS.

Watch the status while it runs (from another SSH session):

```bash
cat ~/mouse.log
# vol=0.0180 mic=ok state=SOUND quiet=7s threshold=0.04
```

## Tuning

All the knobs are constants at the top of `mouse_video.py`:

- **`THRESHOLD`** — loudness that counts as "sound". Lower is more sensitive.
  Set it just above the `vol` you see when the room is quiet. A quiet home is
  around 0.01-0.02; a noisier room needs 0.04 or more. Ours is 0.04.
- **`QUIET_SECONDS`** — how long the room must stay quiet before the mouse comes
  out. Longer makes the mouse shyer and its appearances rarer.
- **`LOUD_HITS_NEEDED`** — consecutive loud reads needed to count as sound;
  raise it to ignore brief spikes.
- **`DEVICE`** — sounddevice index of the mic (see setup).

## Start automatically on boot

The Pi 5 desktop is **labwc**, so autostart goes in
`~/.config/labwc/autostart`. We use a small wrapper script so we can wait for
the desktop and clear any stray instances first.

Create `~/start_mouse.sh`:

```bash
#!/bin/bash
sleep 15
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-0
pkill -f mouse_video.py
pkill mpv
sleep 2
cd /home/sophie
python3 /home/sophie/mouse_video.py
```

Make it executable and register it with labwc:

```bash
chmod +x ~/start_mouse.sh
echo '/home/sophie/start_mouse.sh &' >> ~/.config/labwc/autostart
chmod +x ~/.config/labwc/autostart   # labwc won't run autostart without this
```

Reboot to test. Allow ~1.5-2 minutes for boot plus the 15-second wait before
the mouse scene appears. Confirm it is running:

```bash
pgrep -f mouse_video.py     # prints a PID if it's running
cat ~/mouse.log             # shows live state
```

## Working on this together

Clone the repo (GitHub Desktop: *File -> Clone Repository*; or `git clone`).
To make changes: edit, then commit and push (GitHub Desktop: *Commit to main*,
then *Push origin*). Pull the latest before each session. The owner grants edit
access under *Settings -> Collaborators* on github.com.
