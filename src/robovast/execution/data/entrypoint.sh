#!/bin/bash
set -e

# @@INIT_BLOCK@@

SCENARIO_EXECUTION_PARAMETERS="${SCENARIO_EXECUTION_PARAMETERS:-}"

# Setup
# OUTPUT_DIR holds this job's job-level artifacts (sysinfo, resource monitor,
# logs, rosbag). SCENARIO_OUTPUT_DIR is scenario_execution's -o; per-config
# results land under it via each parameter document's _output_dir.
# In single-config jobs both default to /out (the run directory). In packed
# multi-config jobs /out is the campaign root, so the launcher points OUTPUT_DIR
# at a per-unit subdir (avoiding cross-unit collisions) while SCENARIO_OUTPUT_DIR
# stays /out so per-config results are written to /out/<config>/<run>.
OUTPUT_DIR="${OUTPUT_DIR:-/out}"
SCENARIO_OUTPUT_DIR="${SCENARIO_OUTPUT_DIR:-${OUTPUT_DIR}}"
LOG_DIR="${OUTPUT_DIR}/logs"
mkdir -p "${LOG_DIR}"

# Determine log filename
LOG_FILE="${LOG_DIR}/system.log"

# `_now` and `log`, shared verbatim with secondary_entrypoint.sh (see _LOG_BLOCK in
# robovast/common/execution.py). Both containers' lines must carry the same format for the
# merged run log to place them, so there is one definition rather than two that can drift.
# @@LOG_BLOCK@@

# Everything this script prints -- `log` lines and bare `echo`s alike -- goes to the durable
# artifact from here on, which is earlier than it used to happen. Previously only `log` lines
# were teed, and the redirect sat after the X11 block, so the Xvfb and tool-check output
# reached the *live* log and never the file people read after a failure.
#
# Placed after LOG_DIR exists and before anything logs. The init block stays above it: that
# is what fixuid needs, and it runs before there is a directory to write into.
if [ "$#" -eq 0 ] || [[ "$@" != *"bash"* && "$@" != *"sh"* ]]; then
    # The full tool check runs below, but these two are needed *by the redirect itself*, so
    # a missing one has to fail here and loudly -- otherwise the redirect silently discards
    # every line that would have reported it.
    for _tool in tee stdbuf; do
        command -v "${_tool}" > /dev/null 2>&1 || {
            echo "ERROR: Required tool '${_tool}' not found in container image." >&2
            exit 1
        }
    done
    # `stdbuf -oL` unbuffers tee so the live log panel sees lines as they are printed.
    exec > >(stdbuf -oL tee -a "${LOG_FILE}")
    exec 2>&1
fi

log "Running as UID: $(id -u), GID: $(id -g)..."

# Fail fast if any required tool is missing, rather than wasting a full run and
# only discovering the gap in a post-run step (e.g. 'mc' during the S3 upload).
check_required_tools() {
    local missing=""
    for _tool in "$@"; do
        command -v "${_tool}" > /dev/null 2>&1 || missing="${missing} ${_tool}"
    done
    if [ -n "${missing}" ]; then
        log "ERROR: Required tool(s) not found in container image:${missing}"
        log "ERROR: Rebuild the image with the missing tool(s) installed before running."
        exit 1
    fi
}

# Base tools every run needs, plus mode-specific tools injected via the init block
# (EXTRA_REQUIRED_TOOLS), plus X11 tools only when the virtual display is enabled.
REQUIRED_TOOLS="python3 start-stop-daemon stdbuf tee find ${EXTRA_REQUIRED_TOOLS:-}"
if [ "${ENABLE_X11}" != "false" ]; then
    REQUIRED_TOOLS="${REQUIRED_TOOLS} Xvfb"
fi
check_required_tools ${REQUIRED_TOOLS}

# setup ros2 environment (optional — skipped when ROS is not present). Done before
# the executor check below: the ROS runner (scenario_execution_ros / ros2) only
# lands on PATH once the ROS overlay and the /ws workspace are sourced, so checking
# earlier would spuriously report "no scenario executor" on a ROS image.
ROS_SETUP_ANNOUNCE=log
# @@ROS_SETUP_BLOCK@@

# A scenario executor must be present: ROS2's runner or the plain CLI.
if ! command -v ros2 > /dev/null 2>&1 && ! command -v scenario_execution > /dev/null 2>&1; then
    log "ERROR: No scenario executor found (need 'ros2' or 'scenario_execution'). Rebuild the image."
    exit 1
fi

# Collect system information (default: true). A container-exec diagnostic sets
# COLLECT_SYSINFO=false: it mounts no /config/collect_sysinfo.py, and under `set -e`
# the missing script would abort before the requested command ever ran. A run records
# its host, so nothing but that diagnostic path should disable this.
if [ "${COLLECT_SYSINFO}" != "false" ]; then
  log "Collecting system information..."
  # Replaced with the cluster provider's INSTANCE_TYPE command (get_instance_type_command);
  # left as an empty assignment on the local lane, which has no instance to identify.
  # @@INSTANCE_TYPE_BLOCK@@
  SYSINFO_FILE="${OUTPUT_DIR}/sysinfo.yaml"
  # --distributions alongside it: which distributions are installed HERE, with the entry-point
  # groups they register and the commit a VCS install came from. Recorded in the container
  # because that is the only place the answer exists -- the process that prepares a campaign
  # carries no simulator, so a record built there said "no asset providers" for a campaign whose
  # image had three private ones. Named per container, like resource_usage_main.csv, because in
  # the ROS shape the simulator is a container of its own and so are its providers.
  # node_name comes from the downward API on the cluster lane and is empty on the local
  # one, which has no node to name -- the same shape as INSTANCE_TYPE above.
  python3 /config/collect_sysinfo.py --output "${SYSINFO_FILE}" --distributions "${OUTPUT_DIR}/distributions_main.json" --external "instance_type=${INSTANCE_TYPE}" --external "node_name=${NODE_NAME}" --external "available_cpus=${AVAILABLE_CPUS}" --external "available_mem=${AVAILABLE_MEM}"
else
  log "System information collection disabled (COLLECT_SYSINFO=false)"
fi

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

# `stdbuf -oL` on the redirect above unbuffers TEE, which is not where the buffering is: the workload's
# stdout is now a pipe, so libc block-buffers it at the source in 4-8 KB chunks and tee
# cannot flush what it was never given. The live log panel then goes quiet for a minute
# and dumps a wall of text -- output that is technically complete and useless to watch.
# This image sets PYTHONUNBUFFERED itself, but a campaign may name any image for a
# container, so state it here where it holds for every one of them.
export PYTHONUNBUFFERED=1

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

    # Start built-in daemons
    start-stop-daemon --start --background --make-pidfile --pidfile /tmp/monitor.pid \
        --startas /usr/bin/python3 -- /config/monitor_resources.py "${OUTPUT_DIR}/resource_usage_main.csv"
    log "Started resource monitor (PID=$(cat /tmp/monitor.pid)) -> ${OUTPUT_DIR}/resource_usage_main.csv"

    # The infrastructure recording (/rosout and /clock), deliberately
    # separate from the scenario's own bag_record: this one runs in WALL time for the
    # whole container's life, so it captures the stack coming up before any scenario
    # starts, and /clock recorded here is what relates the two clocks afterwards. Each
    # message's receive time is wall and its content is sim, so postprocessing gets the
    # mapping sampled at clock rate -- including a real-time factor that is not 1, and
    # pauses. The scenario's bag is recorded with use_sim_time, so it cannot carry this.
    #
    # The directory keeps its historical name: `logs/rosout_bag` is the address
    # _ROSBAG_BATCH_MAP, the docs and every existing campaign already use, and renaming
    # it for accuracy would buy a migration and nothing else.
    LOG_TOPICS="${LOG_TOPICS:-/rosout /clock}"
    if command -v ros2 > /dev/null 2>&1 && [ -n "${LOG_TOPICS}" ]; then
        start-stop-daemon --start --background --make-pidfile --pidfile /tmp/rosbag.pid \
            --startas /bin/bash -- -c "exec ros2 bag record -o ${OUTPUT_DIR}/logs/rosout_bag --storage mcap --topics ${LOG_TOPICS}"
        log "Started rosbag recording ${LOG_TOPICS} (PID=$(cat /tmp/rosbag.pid)) -> ${OUTPUT_DIR}/logs/rosout_bag"
    fi

    # @@POST_RUN_BLOCK@@

    SCENARIO_FILE="${SCENARIO_FILE:-scenario.osc}"
    # Parameter file is a single-config scenario.config by default. In
    # multi-config-per-job mode robovast supplies a multi-document parameter
    # file (one document per packed configuration) and sets
    # OUTPUT_RESULT_PER_SCENARIO=true so scenario_execution writes a separate
    # test.xml into each configuration's _output_dir subdirectory.
    SCENARIO_PARAMETER_FILE="${SCENARIO_PARAMETER_FILE:-/config/scenario.config}"
    PER_SCENARIO_PARAM=""
    if [ "${OUTPUT_RESULT_PER_SCENARIO}" = "true" ]; then
        PER_SCENARIO_PARAM="--output-result-per-scenario"
    fi
    # Optional simulation backend (execution.simulation in the .vast). Required by
    # scenarios using wait_for_simulation_end(); passed as --simulation <module:Class>.
    SIMULATION="${SIMULATION:-}"
    SIMULATION_PARAM=""
    if [ -n "${SIMULATION}" ]; then
        SIMULATION_PARAM="--simulation ${SIMULATION}"
    fi
    # Behaviour tree status log: scenario_execution writes <output-dir>/behaviors.jsonl
    # itself, with or without ROS. Always on, and defaulted here as well, so a container
    # started by hand records its tree too -- that is the run nobody can go back and
    # re-instrument.
    BT_LOG="${BT_LOG:-true}"
    BT_LOG_PARAM=""
    if [ "${BT_LOG}" = "true" ]; then
        BT_LOG_PARAM="--bt-log"
    fi
    # Runner selection (execution.mode in the .vast):
    #   ros2 -> ROS runner:      `ros2 run scenario_execution_ros scenario_execution_ros`
    #   base -> non-ROS runner:  `ros2 run scenario_execution scenario_execution`
    #           (the ROS image builds the base package into the workspace, reachable
    #           via `ros2 run`, not as a bare binary on PATH)
    #   auto -> detect: the ROS runner when ros2 is on PATH, otherwise the bare
    #           `scenario_execution` console script (pip/non-ROS images only)
    SCENARIO_MODE="${SCENARIO_MODE:-auto}"
    if [ "${SCENARIO_MODE}" = "ros2" ]; then
        RUNNER_CMD="ros2 run scenario_execution_ros scenario_execution_ros"
    elif [ "${SCENARIO_MODE}" = "base" ]; then
        RUNNER_CMD="ros2 run scenario_execution scenario_execution"
    elif command -v ros2 > /dev/null 2>&1; then
        RUNNER_CMD="ros2 run scenario_execution_ros scenario_execution_ros"
    else
        RUNNER_CMD="scenario_execution"
    fi
    if [ -e "${SCENARIO_PARAMETER_FILE}" ]; then
        log "Starting scenario execution (mode=${SCENARIO_MODE}) with config file..."
        log "Commandline: ${RUNNER_CMD} -o ${SCENARIO_OUTPUT_DIR} /config/${SCENARIO_FILE} ${POST_COMMAND_PARAM} --scenario-parameter-file ${SCENARIO_PARAMETER_FILE} ${PER_SCENARIO_PARAM} ${SIMULATION_PARAM} ${BT_LOG_PARAM} ${SCENARIO_EXECUTION_PARAMETERS}"
        exec ${RUNNER_CMD} -o ${SCENARIO_OUTPUT_DIR} /config/${SCENARIO_FILE} ${POST_COMMAND_PARAM} --scenario-parameter-file ${SCENARIO_PARAMETER_FILE} ${PER_SCENARIO_PARAM} ${SIMULATION_PARAM} ${BT_LOG_PARAM} ${SCENARIO_EXECUTION_PARAMETERS}
    else
        log "Starting scenario execution (mode=${SCENARIO_MODE}) without config file..."
        exec ${RUNNER_CMD} -o ${SCENARIO_OUTPUT_DIR} /config/${SCENARIO_FILE} ${POST_COMMAND_PARAM} ${SIMULATION_PARAM} ${BT_LOG_PARAM} ${SCENARIO_EXECUTION_PARAMETERS}
    fi
fi
