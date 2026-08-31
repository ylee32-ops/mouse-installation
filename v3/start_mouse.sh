#!/bin/bash
# Autostart wrapper for the mouse installation on Raspberry Pi 5 (labwc).
# Registered via ~/.config/labwc/autostart (which must be executable —
# see TROUBLESHOOTING.md #6). See README.md.
sleep 15                                   # wait for the desktop to be ready
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-0
pkill -f mouse_video.py                    # clear any stray instances
pkill mpv
sleep 2

# Run from wherever this script lives, so the repo can sit anywhere.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
python3 mouse_video.py
