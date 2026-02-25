#!/bin/bash
set -e

eval $(fixuid -q)

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

log "Running as UID: $(id -u), GID: $(id -g)..."

# Collect system information (non-fatal)
log "Collecting system information..."
INSTANCE_TYPE=""
SYSINFO_FILE="${OUTPUT_DIR}/sysinfo.yaml"
python3 /config/collect_sysinfo.py --output "${SYSINFO_FILE}" --external "instance_type=${INSTANCE_TYPE}" --external "available_cpus=${AVAILABLE_CPUS}" --external "available_mem=${AVAILABLE_MEM}"

# setup ros2 environment
log "Setting up ROS2 environment..."
source "/opt/ros/$ROS_DISTRO/setup.bash" --
source "/ws/install/setup.bash" --

# Check if X11 is enabled (default: true for backward compatibility)
if [ "${ENABLE_X11}" != "false" ]; then
  log "Starting X11 virtual display..."
  if [ -z "${DISPLAY}" ]; then
    export DISPLAY=:0
  fi

  if [ -S "/tmp/.X11-unix/X${DISPLAY/:/}" ]; then
    echo "x11 already running..."
  else

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
  fi
else
  log "X11 disabled - skipping virtual display setup"
fi

# Only redirect output to log file if not running an interactive shell
if [ "$#" -eq 0 ] || [[ "$@" != *"bash"* && "$@" != *"sh"* ]]; then
    # Run the actual command and capture output
    # Using unbuffered tee for real-time output
    exec > >(stdbuf -oL tee -a "${LOG_FILE}")
    exec 2>&1
fi

log "Entrypoint script initialized"

if [ "$#" -ne 0 ]; then
    log "Executing custom command: $@"
    exec "$@"
else
    # Validate PRE_COMMAND exists if specified
    if [ -n "${PRE_COMMAND}" ]; then
        if [ -e "${PRE_COMMAND}" ]; then
            log "Executing pre-command: ${PRE_COMMAND}"
            source "${PRE_COMMAND}"
        else
            log "ERROR: Pre-command '${PRE_COMMAND}' does not exist."
            exit 1
        fi
    fi

    # @@POST_RUN_BLOCK@@

    if [ -e /config/scenario.config ]; then
        log "Starting scenario execution with config file..."
        exec ros2 run scenario_execution_ros scenario_execution_ros -o ${OUTPUT_DIR} /config/scenario.osc ${POST_COMMAND_PARAM} --scenario-parameter-file /config/scenario.config ${SCENARIO_EXECUTION_PARAMETERS}
    else
        log "Starting scenario execution without config file..."
        exec ros2 run scenario_execution_ros scenario_execution_ros -o ${OUTPUT_DIR} /config/scenario.osc ${POST_COMMAND_PARAM} ${SCENARIO_EXECUTION_PARAMETERS}
    fi
fi
