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
# Set up the ROS overlay first (when present) so the ROS server runner
# (scenario_execution_server_ros / ros2) is on PATH for the check below: it only
# lands there once the ROS overlay and the /ws workspace are sourced, so checking
# earlier would spuriously report "no scenario-execution server" on a ROS image.
if [ -n "$ROS_DISTRO" ] && [ -f "/opt/ros/$ROS_DISTRO/setup.bash" ]; then
    log "Setting up ROS2 environment..."
    source "/opt/ros/$ROS_DISTRO/setup.bash"
    if [ -f "/ws/install/setup.bash" ]; then
        source "/ws/install/setup.bash"
    fi
fi

# The scenario-execution server runner: ROS2's or the plain CLI. Only required when
# this container IS the server; one running its own command (a simulator, a stack
# RoboVAST does not drive) has no reason to carry scenario-execution at all, and
# demanding it would contradict the promise that such an image can be vanilla.
if [ -z "${ROBOVAST_CONTAINER_COMMAND}" ] \
   && ! command -v ros2 > /dev/null 2>&1 \
   && ! command -v scenario_execution_server > /dev/null 2>&1; then
    log "ERROR: No scenario-execution server found (need 'ros2' or 'scenario_execution_server'). Rebuild the image."
    exit 1
fi

exec > >(stdbuf -oL tee -a "${LOG_FILE}")
exec 2>&1

SOCKET="/ipc/${CONTAINER_NAME}"

# Start resource monitor
python3 /config/monitor_resources.py "${OUTPUT_DIR}/resource_usage_${CONTAINER_NAME}.csv" &
log "Started resource monitor (PID=$!) -> ${OUTPUT_DIR}/resource_usage_${CONTAINER_NAME}.csv"

# A container that declares its own command runs THAT, with everything above already
# done for it: the ROS overlay sourced, stdout teed into the job's log directory, and
# the resource monitor running. Exec'ing the command directly as the container's
# entrypoint -- which is what used to happen -- skipped all three. The ROS one is not a
# nicety: a colcon package like the MuJoCo bridge only reaches PYTHONPATH once
# /opt/ros and /ws/install are sourced, so `rst sim --ros` died instantly with
# "unknown plugin 'ros2_bridge'" while the scenario waited out its /scan timeout with
# no log anywhere to say why. Any simulator backend would have hit the same wall, so
# this belongs here and not in one backend's command string.
if [ -n "${ROBOVAST_CONTAINER_COMMAND}" ]; then
    log "Starting container command: ${ROBOVAST_CONTAINER_COMMAND}"
    exec ${ROBOVAST_CONTAINER_COMMAND}
fi

if command -v ros2 > /dev/null 2>&1; then
    log "Starting scenario-execution-server-ros on socket '${SOCKET}'..."
    exec ros2 run scenario_execution_server_ros scenario_execution_server_ros --watchdog ${WATCHDOG_TIMEOUT} --connect-timeout ${CONNECT_TIMEOUT} --socket "${SOCKET}"
else
    log "Starting scenario-execution-server on socket '${SOCKET}'..."
    exec scenario_execution_server --watchdog ${WATCHDOG_TIMEOUT} --connect-timeout ${CONNECT_TIMEOUT} --socket "${SOCKET}"
fi