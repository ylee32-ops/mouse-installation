# Sound-Reactive Mouse Installation

A screen-based interactive installation. A shy on-screen mouse lives in a hole
in the wall. When the room is quiet for long enough, it creeps out to nibble a
piece of cheese (a paper cheese is hung in front of the screen). The moment it
hears a sound — a clap, a spoken word — it dashes back into the wall.

The piece is a small state machine that swaps between short stop-motion clips
filmed with plushies, driven by the room's sound level.

## How it works

The animation plays as full-quality video clips through **mpv**, which suits a
Raspberry Pi 5 (hardware video decoding, plenty of RAM). Sound level is read
from a USB microphone using **`arecord`** (ALSA); it is stable and doesn't fight
mpv over the audio device. The microphone reads the current sound *level* only —
no audio is recorded or saved.

### State machine

| State        | Plays                     | Leaves when                                 |
|--------------|---------------------------|---------------------------------------------|
| `SOUND`      | `v1_quiet.mp4` (loop)     | quiet for `QUIET_SECONDS` → `COMING_OUT`    |
| `COMING_OUT` | `v2_outtoeat.mp4` (once)  | clip ends → `EATING`; sound → `RUNBACK`     |
| `EATING`     | `v3_eatingloop.mp4` (loop)| sound → `RUNBACK`                           |
| `RUNBACK`    | `v4_runback.mp4` (once)   | clip ends → `COMING_OUT` or `SOUND`         |

`v4_runback.mp4` always plays to the end before the next state is chosen, so the
flee never gets cut off mid-scramble.

## Hardware

- Raspberry Pi 5 (recommended; handles fullscreen video comfortably)
- HDMI screen
- USB microphone / USB audio input
- Official 5V power supply

## Software setup

On Raspberry Pi OS:

```bash
sudo apt update
sudo apt install mpv python3-numpy alsa-utils
```

Find the microphone's card/device number:

```bash
arecord -l
```

Set `CARD` at the top of `mouse_video.py` to match (for example `plughw:1,0`
for card 1, device 0). **This can differ between Pis, so check it on the machine
you actually run on.**

## The video files

Put the four clips next to `mouse_video.py`:

```
v1_quiet.mp4        empty scene (no mouse)
v2_outtoeat.mp4     mouse emerges toward the cheese
v3_eatingloop.mp4   mouse eating (loops seamlessly)
v4_runback.mp4      mouse flees back into the wall
```

These are large, so they are **not** stored in the repository (see
`.gitignore`). Keep them together somewhere shared (a drive, a USB stick) and
copy them onto the Pi alongside the script.

## Running

```bash
DISPLAY=:0 python3 mouse_video.py
```

The `DISPLAY=:0` prefix tells it to draw to the Pi's attached screen when you
start it over SSH.

A status line prints once a second so you can tune the sound detection:

```
vol=0.0180 state=SOUND quiet=7s
```

## Tuning

All the knobs are constants at the top of `mouse_video.py`:

- **`THRESHOLD`** — loudness that counts as "sound". Lower is more sensitive.
  Set it just above the `vol` you see when the room is quiet.
- **`QUIET_SECONDS`** — how long the room must stay quiet before the mouse comes
  out. Longer makes the mouse shyer and its appearances rarer.
- **`LOUD_HITS_NEEDED`** — consecutive loud readings needed to count as sound;
  raise it to ignore brief background spikes.

## Working on this together

This repo is set up so teammates can update it.

**First time (each person):** clone the repo — in GitHub Desktop, *File → Clone
Repository* and pick this one; or on the command line:

```bash
git clone https://github.com/YOUR_USERNAME/mouse-installation.git
```

**To make changes:** edit the files, then in GitHub Desktop write a short
summary and click *Commit to main*, then *Push origin*. On the command line:

```bash
git add .
git commit -m "what you changed"
git push
```

**Before you start each session**, pull the latest first (GitHub Desktop:
*Fetch/Pull origin*; command line `git pull`) so you have your teammate's
updates.

To give someone edit access, the repo owner adds them under
*Settings → Collaborators* on github.com.
