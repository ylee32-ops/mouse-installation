#!/bin/bash
# Autostart wrapper for the mouse installation on Raspberry Pi 5 (labwc).
# Registered via ~/.config/labwc/autostart. See README.md.
sleep 15                                   # wait for the desktop to be ready
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-0
pkill -f mouse_video.py                    # clear any stray instances
pkill mpv
sleep 2
cd /home/sophie
python3 /home/sophie/mouse_video.py
