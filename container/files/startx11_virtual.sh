#!/bin/bash -e
if [ -z "${DISPLAY}" ]; then
  export DISPLAY=:0
fi

if [ -S "/tmp/.X11-unix/X${DISPLAY/:/}" ]; then
  echo "x11 already running..."
  exit 0
fi

mkdir -p /tmp/runtime-user 2>/dev/null || true
mkdir -p /tmp/.X11-unix 2>/dev/null || true
chmod 1777 /tmp/.X11-unix 2>/dev/null || true
ln -snf /dev/ptmx /dev/tty7 2>/dev/null || true

Xvfb tty7 -noreset -dpi "${DPI}" +extension "RANDR" +extension "RENDER" +extension "MIT-SHM" -screen ${DISPLAY} ${SIZEW}x${SIZEH}x${CDEPTH} "${DISPLAY}" 2>/dev/null &

echo -n "Waiting for X socket..."
until [ -S "/tmp/.X11-unix/X${DISPLAY/:/}" ]; do sleep 1; done
echo "DONE"

if [ -n "${NOVNC_ENABLE}" ]; then
  echo "Starting VNC..."
  x11vnc -display "${DISPLAY}" -shared -forever -repeat -xkb -snapfb -threads -xrandr "resize" -rfbport 5900 -bg
  /opt/noVNC/utils/novnc_proxy --vnc localhost:5900 --listen 8080 --heartbeat 10 &
fi

if [ -n "${WINDOW_MANAGER_ENABLE}" ]; then
  echo "Starting Window Manager..."
  openbox &
fi