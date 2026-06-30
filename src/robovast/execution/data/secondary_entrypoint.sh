#!/bin/bash
set -e

# @@INIT_BLOCK@@

WATCHDOG_TIMEOUT=3
CONNECT_TIMEOUT=15

# OUTPUT_DIR holds this job's job-level artifacts; in packed multi-config jobs
# the launcher points it at a per-unit subdir of /out to avoid cross-unit
# collisions (defaults to /out for single-config jobs).
OUTPUT_DIR="${OUTPUT_DIR:-/out}"
LOG_DIR="${OUTPUT_DIR}/logs"
mkdir -p "${LOG_DIR}"

LOG_FILE="${LOG_DIR}/system_${CONTAINER_NAME}.log"

log() {
    echo "$@" | tee -a "${LOG_FILE}"
}

log "Secondary container starting ($(hostname))..."
log "Running as UID: $(id -u), GID: $(id -g)..."

# Fail fast if a required tool is missing instead of dying mid-startup.
for _tool in python3 stdbuf tee; do
    command -v "${_tool}" > /dev/null 2>&1 || {
        log "ERROR: Required tool '${_tool}' not found in container image. Rebuild the image."
        exit 1
    }
done
# The scenario-execution server runner: ROS2's or the plain CLI.
if ! command -v ros2 > /dev/null 2>&1 && ! command -v scenario_execution_server > /dev/null 2>&1; then
    log "ERROR: No scenario-execution server found (need 'ros2' or 'scenario_execution_server'). Rebuild the image."
    exit 1
fi

if [ -n "$ROS_DISTRO" ] && [ -f "/opt/ros/$ROS_DISTRO/setup.bash" ]; then
    log "Setting up ROS2 environment..."
    source "/opt/ros/$ROS_DISTRO/setup.bash"
    if [ -f "/ws/install/setup.bash" ]; then
        source "/ws/install/setup.bash"
    fi
fi

exec > >(stdbuf -oL tee -a "${LOG_FILE}")
exec 2>&1

SOCKET="/ipc/${CONTAINER_NAME}"

# Start resource monitor
python3 /config/monitor_resources.py "${OUTPUT_DIR}/resource_usage_${CONTAINER_NAME}.csv" &
log "Started resource monitor (PID=$!) -> ${OUTPUT_DIR}/resource_usage_${CONTAINER_NAME}.csv"

if command -v ros2 > /dev/null 2>&1; then
    log "Starting scenario-execution-server-ros on socket '${SOCKET}'..."
    exec ros2 run scenario_execution_server_ros scenario_execution_server_ros --watchdog ${WATCHDOG_TIMEOUT} --connect-timeout ${CONNECT_TIMEOUT} --socket "${SOCKET}"
else
    log "Starting scenario-execution-server on socket '${SOCKET}'..."
    exec scenario_execution_server --watchdog ${WATCHDOG_TIMEOUT} --connect-timeout ${CONNECT_TIMEOUT} --socket "${SOCKET}"
fi