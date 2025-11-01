#!/bin/bash
set -e

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
/startx11_virtual.sh

# Run the actual command and capture output
# Using unbuffered tee for real-time output
exec > >(stdbuf -oL tee -a "${LOG_FILE}")
exec 2>&1

log "Entrypoint script initialized"

# Write run information
echo "RUN_ID: $RUN_ID" > ${OUTPUT_DIR}/run.yaml
echo "RUN_NUM: $RUN_NUM" >> ${OUTPUT_DIR}/run.yaml
echo "SCENARIO_ID: $SCENARIO_ID" >> ${OUTPUT_DIR}/run.yaml
echo "SCENARIO_CONFIG: $SCENARIO_CONFIG" >> ${OUTPUT_DIR}/run.yaml

log "Copying configuration files..."
cp -r /config/* ${OUTPUT_DIR}/

if [ "$#" -ne 0 ]; then
    log "Executing custom command: $@"
    exec "$@"
else
    if [ -e /config/scenario.variant ]; then
        log "Starting scenario execution with variant file..."
        exec ros2 run scenario_execution_ros scenario_execution_ros -o ${OUTPUT_DIR} /config/scenario.osc --scenario-parameter-file /config/scenario.variant ${SCENARIO_EXECUTION_PARAMETERS}
    else
        log "Starting scenario execution without variant file..."
        exec ros2 run scenario_execution_ros scenario_execution_ros -o ${OUTPUT_DIR} /config/scenario.osc ${SCENARIO_EXECUTION_PARAMETERS}
    fi
fi
