#!/bin/bash
set -e

# Handle dynamic user creation for arbitrary UIDs
CURRENT_UID=$(id -u)
CURRENT_GID=$(id -g)

if [ "$CURRENT_UID" != "0" ]; then
    # Running as non-root, check if user exists
    if ! id -u "$CURRENT_UID" &>/dev/null; then
        echo "Creating dynamic user with UID=$CURRENT_UID and GID=$CURRENT_GID"

        # Create group if it doesn't exist
        if ! getent group "$CURRENT_GID" &>/dev/null; then
            groupadd -g "$CURRENT_GID" dynamicgroup
        fi

        # Create user and add to sudo group
        useradd -u "$CURRENT_UID" -g "$CURRENT_GID" -G sudo -m -s /bin/bash dynamicuser

        echo "Dynamic user created successfully"
    fi
fi

# Setup
OUTPUT_DIR="/out"
LOG_DIR="${OUTPUT_DIR}/logs"
mkdir -p "${LOG_DIR}"

# Determine log filename
LOG_FILE="${LOG_DIR}/system.log"

# Function to log to both console and file
log() {
    echo "$@" | tee -a "${LOG_FILE}"
}

# setup ros2 environment
log "Setting up ROS2 environment..."
source "/opt/ros/$ROS_DISTRO/setup.bash" --
source "/ws/install/setup.bash" --

log "Starting X11 virtual display..."
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

# Only redirect output to log file if not running an interactive shell
if [ "$#" -eq 0 ] || [[ "$@" != *"bash"* && "$@" != *"sh"* ]]; then
    # Run the actual command and capture output
    # Using unbuffered tee for real-time output
    exec > >(stdbuf -oL tee -a "${LOG_FILE}")
    exec 2>&1
fi

log "Entrypoint script initialized"

# Write run information
echo "RUN_ID: $RUN_ID" > ${OUTPUT_DIR}/run.yaml
echo "RUN_NUM: $RUN_NUM" >> ${OUTPUT_DIR}/run.yaml
echo "SCENARIO_ID: $SCENARIO_ID" >> ${OUTPUT_DIR}/run.yaml
echo "SCENARIO_CONFIG: $SCENARIO_CONFIG" >> ${OUTPUT_DIR}/run.yaml

if [ -d /config ]; then
  log "Copying configuration files..."
  cp -r /config/* ${OUTPUT_DIR}/
fi

if [ "$#" -ne 0 ]; then
    log "Executing custom command: $@"
    exec "$@"
else
    if [ -e /config/prepare_test.sh ]; then
        log "Sourcing custom prepare script..."
        source /config/prepare_test.sh
    fi
    if [ -e /config/scenario.config ]; then
        log "Starting scenario execution with config file..."
        exec ros2 run scenario_execution_ros scenario_execution_ros -o ${OUTPUT_DIR} /config/scenario.osc --scenario-parameter-file /config/scenario.config ${SCENARIO_EXECUTION_PARAMETERS}
    else
        log "Starting scenario execution without config file..."
        exec ros2 run scenario_execution_ros scenario_execution_ros -o ${OUTPUT_DIR} /config/scenario.osc ${SCENARIO_EXECUTION_PARAMETERS}
    fi
fi
