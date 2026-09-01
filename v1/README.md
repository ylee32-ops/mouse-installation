# Sound-Reactive Mouse Installation

A screen-based interactive installation. A shy on-screen mouse lives in a hole
in the wall. When the room is quiet for long enough, it creeps out to nibble a
piece of cheese hung in front of the screen. The moment it hears a sound — a
clap, a spoken word — it dashes back into the wall.

## How it works

The mouse is animated with short stop-motion clips filmed with plushies, played
fullscreen through `mpv`. A small program (`mouse_video.py`) runs a state
machine that swaps between the clips based on the room's sound level, read from
a USB microphone. When it's quiet the mouse comes out to eat; when it hears
something it flees.

The four clips are:

```
v1_quiet.mp4        empty scene (no mouse)
v2_outtoeat.mp4     mouse emerges toward the cheese
v3_eatingloop.mp4   mouse eating (loops)
v4_runback.mp4      mouse flees back into the wall
```

The microphone reads the current sound *level* only — nothing is recorded or
saved.

It runs on a Raspberry Pi connected to a screen, with a USB microphone for
sound.

## Running it

The program needs `mpv`, `sounddevice`, and `numpy`, and the four `.mp4` clips
sitting next to it. Then it runs on the Pi and, once set up, starts
automatically on boot.

Full setup, tuning, and autostart instructions — plus every problem we hit and
how we solved it — are in **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)**. Read it
before running or changing anything; it will save you a lot of time.

## Working on this together

Clone the repo, make your changes, then commit and push. Pull the latest before
each session. The video files are large and are not stored here — share them
separately and keep them next to the program on the Pi.
