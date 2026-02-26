#!/bin/bash
set -e

# @@INIT_BLOCK@@

OUTPUT_DIR="/out"
LOG_DIR="${OUTPUT_DIR}/logs"
mkdir -p "${LOG_DIR}"

LOG_FILE="${LOG_DIR}/system_${CONTAINER_NAME}.log"

log() {
    echo "$@" | tee -a "${LOG_FILE}"
}

log "Secondary container starting ($(hostname))..."
log "Running as UID: $(id -u), GID: $(id -g)..."

log "Setting up ROS2 environment..."
source "/opt/ros/$ROS_DISTRO/setup.bash"
source "/ws/install/setup.bash"

exec > >(stdbuf -oL tee -a "${LOG_FILE}")
exec 2>&1

SOCKET="/ipc/${CONTAINER_NAME}"
log "Starting scenario-execution-server-ros on socket '${SOCKET}'..."
exec ros2 run scenario_execution_server_ros scenario_execution_server_ros --watchdog 3 --socket "${SOCKET}"
