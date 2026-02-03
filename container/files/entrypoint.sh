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

# Only redirect output to log file if not running an interactive shell
if [ "$#" -eq 0 ] || [[ "$@" != *"bash"* && "$@" != *"sh"* ]]; then
    # Run the actual command and capture output
    # Using unbuffered tee for real-time output
    exec > >(stdbuf -oL tee -a "${LOG_FILE}")
    exec 2>&1
fi

log "Entrypoint script initialized"

# Generate timestamp for run
TIMESTAMP=$(date +"%Y-%m-%d-%H%M%S")
START_DATE=$(date +"%Y-%m-%dT%H%M%S")

# Create descriptive RUN_ID with scenario config, run number, and timestamp
DESCRIPTIVE_RUN_ID="${SCENARIO_CONFIG}-run-${RUN_NUM}-${TIMESTAMP}"

# Write run information
echo "RUN_ID: $DESCRIPTIVE_RUN_ID" > ${OUTPUT_DIR}/run.yaml
echo "RUN_NUM: $RUN_NUM" >> ${OUTPUT_DIR}/run.yaml
echo "START_DATE: $START_DATE" >> ${OUTPUT_DIR}/run.yaml
echo "# END_DATE: # Can be added after test completion" >> ${OUTPUT_DIR}/run.yaml
echo "SCENARIO_ID: $SCENARIO_CONFIG" >> ${OUTPUT_DIR}/run.yaml

if [ -d /config ]; then
  log "Copying configuration files..."
  cp -r /config/* ${OUTPUT_DIR}/
fi

if [ "$#" -ne 0 ]; then
    log "Executing custom command: $@"
    exec "$@"
else
    # Run scenario execution and capture exit code
    if [ -e /config/scenario.config ]; then
        log "Starting scenario execution with config file..."
        ros2 run scenario_execution_ros scenario_execution_ros -o ${OUTPUT_DIR} /config/scenario.osc --scenario-parameter-file /config/scenario.config ${SCENARIO_EXECUTION_PARAMETERS}
        EXIT_CODE=$?
    else
        log "Starting scenario execution without config file..."
        ros2 run scenario_execution_ros scenario_execution_ros -o ${OUTPUT_DIR} /config/scenario.osc ${SCENARIO_EXECUTION_PARAMETERS}
        EXIT_CODE=$?
    fi
    
    # Add END_DATE to run.yaml
    END_DATE=$(date +"%Y-%m-%dT%H%M%S")
    echo "END_DATE: $END_DATE" >> ${OUTPUT_DIR}/run.yaml
    
    # Convert test.xml to test.yaml if it exists
    if [ -f "${OUTPUT_DIR}/test.xml" ]; then
        log "Converting test.xml to test.yaml..."
        python3 /xml_to_yaml_converter.py ${OUTPUT_DIR}
    fi
    
    # Exit with the same code as the scenario execution
    exit $EXIT_CODE
fi
