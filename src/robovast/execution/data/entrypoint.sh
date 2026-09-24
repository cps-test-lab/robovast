#!/bin/bash
set -e

# @@INIT_BLOCK@@

SCENARIO_EXECUTION_PARAMETERS="${SCENARIO_EXECUTION_PARAMETERS:-}"

# Setup
# OUTPUT_DIR holds this job's job-level artifacts (sysinfo, resource monitor,
# logs, rosbag). SCENARIO_OUTPUT_DIR is scenario_execution's -o; the run's
# results land under it via its parameter document's _output_dir.
# Both default to /out. On the cluster /out is the campaign root, so the
# launcher points OUTPUT_DIR at the job's subdir while SCENARIO_OUTPUT_DIR
# stays /out so the run's results are written to /out/<config>/<run>.
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
# artifact from here on. Teeing only `log` lines, or placing the redirect after the X11
# block, leaves the Xvfb and tool-check output in the *live* log and out of the file people
# read after a failure.
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
# only discovering the gap in a post-run step.
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
  # left as an empty assignment where no provider names an instance.
  # @@INSTANCE_TYPE_BLOCK@@
  SYSINFO_FILE="${OUTPUT_DIR}/sysinfo.yaml"
  # --distributions alongside it: which distributions are installed HERE, with the entry-point
  # groups they register and the commit a VCS install came from. Recorded in the container
  # because that is the only place the answer exists -- the process that prepares a campaign
  # carries no simulator, so a record built there said "no asset providers" for a campaign whose
  # image had three private ones. Named per container, like resource_usage_main.csv, because in
  # the ROS shape the simulator is a container of its own and so are its providers.
  # NODE_NAME comes from the downward API and is empty in a container no Job placed (a
  # diagnostic exec), which has no node to name -- the same shape as INSTANCE_TYPE above. It is passed
  # as --node-name rather than --external because collect_sysinfo HASHES it: this file
  # ships inside the campaign archive, so the name itself must not reach it.
  python3 /config/collect_sysinfo.py --output "${SYSINFO_FILE}" --distributions "${OUTPUT_DIR}/distributions_main.json" --external "instance_type=${INSTANCE_TYPE}" --node-name "${NODE_NAME}" --external "available_cpus=${AVAILABLE_CPUS}" --external "available_mem=${AVAILABLE_MEM}"
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

    # Both recorders below write through and split every 10 s (RECORD_OPTIONS): each message
    # reaches the bag file as it is written, a closed segment is complete, and the open one is
    # readable up to its last complete record -- so a bag is readable while the run runs.
    # The preset is shipped with the run scripts (robovast.common.execution.MCAP_STORAGE_CONFIG).
    RECORD_OPTIONS="--storage mcap --max-cache-size 0 --storage-config-file /config/mcap_writethrough.yaml -d 10"

    # The infrastructure recording (/rosout and /clock), deliberately
    # separate from the scenario's recording below: this one runs in WALL time for the
    # whole container's life, so it captures the stack coming up before any scenario
    # starts, and /clock recorded here is what relates the two clocks afterwards. Each
    # message's receive time is wall and its content is sim, so postprocessing gets the
    # mapping sampled at clock rate -- including a real-time factor that is not 1, and
    # pauses. The scenario's bag may be recorded with use_sim_time, so it cannot carry this.
    #
    # The directory keeps its historical name: `logs/rosout_bag` is the address
    # _ROSBAG_BATCH_MAP, the docs and every existing campaign already use, and renaming
    # it for accuracy would buy a migration and nothing else.
    LOG_TOPICS="${LOG_TOPICS:-/rosout /clock}"
    if command -v ros2 > /dev/null 2>&1 && [ -n "${LOG_TOPICS}" ]; then
        start-stop-daemon --start --background --make-pidfile --pidfile /tmp/rosbag.pid \
            --startas /bin/bash -- -c "exec ros2 bag record -o ${OUTPUT_DIR}/logs/rosout_bag ${RECORD_OPTIONS} --topics ${LOG_TOPICS}"
        log "Started rosbag recording ${LOG_TOPICS} (PID=$(cat /tmp/rosbag.pid)) -> ${OUTPUT_DIR}/logs/rosout_bag"
    fi

    # The scenario's recording, <run>/rosbag2: started here, before the runner, rather than
    # by a bag_record action in the scenario, so that what a run records is a property of
    # the campaign (its `recording:` block, arriving as the RECORD_* variables) and not of
    # the scenario text. The bag goes into the run's directory, which a campaign job names
    # (RUN_OUTPUT_DIR); a container started outside a campaign -- a scenario tried in an
    # image -- has no run directory and records the infrastructure bag alone.
    #
    # RECORD_TOPICS is `all` or space-separated entries, a regex when it starts with `^`:
    # names go to --topics, regexes joined to one -e pattern. RECORD_EXCLUDE is one regex,
    # RECORD_EXCLUDE_TYPES space-separated type names, RECORD_USE_SIM_TIME true or false.
    # Absent, everything is recorded in wall time.
    scenario_record_args() {
        SCENARIO_RECORD_ARGS=(bag record -o "${RUN_OUTPUT_DIR}/rosbag2" ${RECORD_OPTIONS})
        if [ "${RECORD_USE_SIM_TIME:-false}" = "true" ]; then
            SCENARIO_RECORD_ARGS+=(--use-sim-time)
        fi
        local _topics="${RECORD_TOPICS:-all}" _names="" _regex="" _entry
        if [ "${_topics}" = "all" ]; then
            SCENARIO_RECORD_ARGS+=(-a)
        else
            for _entry in ${_topics}; do
                case "${_entry}" in
                    ^*) _regex="${_regex:+${_regex}|}${_entry}" ;;
                    *)  _names="${_names} ${_entry}" ;;
                esac
            done
            if [ -n "${_names}" ]; then
                SCENARIO_RECORD_ARGS+=(--topics ${_names})
            fi
            if [ -n "${_regex}" ]; then
                SCENARIO_RECORD_ARGS+=(-e "${_regex}")
            fi
        fi
        if [ -n "${RECORD_EXCLUDE:-}" ]; then
            SCENARIO_RECORD_ARGS+=(--exclude-regex "${RECORD_EXCLUDE}")
        fi
        if [ -n "${RECORD_EXCLUDE_TYPES:-}" ]; then
            SCENARIO_RECORD_ARGS+=(--exclude-topic-types ${RECORD_EXCLUDE_TYPES})
        fi
    }
    start_scenario_recorder() {
        scenario_record_args
        mkdir -p "${RUN_OUTPUT_DIR}"
        # --no-close: the recorder's own output (the topics it subscribed, or why it refused
        # its arguments) belongs in the run log, not in /dev/null.
        log "Starting scenario recording: ros2 ${SCENARIO_RECORD_ARGS[*]}"
        start-stop-daemon --start --background --no-close --make-pidfile --pidfile /tmp/scenario_bag.pid \
            --startas "$(command -v ros2)" -- "${SCENARIO_RECORD_ARGS[@]}"
        # A recorder that refuses its arguments exits at once, and a daemon that died leaves
        # the run looking fine with no bag. So wait for the bag to open, and fail the run if
        # the recorder is gone before it did. The daemon writes its own pidfile after the
        # fork, so the pid is read once it is there rather than assumed to be.
        local _t=0 _pid=""
        while [ ! -d "${RUN_OUTPUT_DIR}/rosbag2" ]; do
            [ -n "${_pid}" ] || _pid="$(cat /tmp/scenario_bag.pid 2>/dev/null)"
            if [ -n "${_pid}" ] && ! kill -0 "${_pid}" 2>/dev/null; then
                log "ERROR: The scenario recorder (PID=${_pid}) exited before it opened ${RUN_OUTPUT_DIR}/rosbag2; its output is above."
                exit 1
            fi
            if [ ${_t} -ge 300 ]; then
                log "WARNING: The scenario recorder has not opened ${RUN_OUTPUT_DIR}/rosbag2 after 30 s; continuing."
                break
            fi
            sleep 0.1; _t=$((_t + 1))
        done
        log "Scenario recording -> ${RUN_OUTPUT_DIR}/rosbag2 (PID=$(cat /tmp/scenario_bag.pid 2>/dev/null))"
    }
    if [ -z "${RUN_OUTPUT_DIR:-}" ]; then
        log "No scenario recording: no run directory (RUN_OUTPUT_DIR) outside a campaign job."
    elif ! command -v ros2 > /dev/null 2>&1; then
        log "No scenario recording: no ros2 in this image."
    else
        start_scenario_recorder
    fi

    # The post-run block: the cleanup hooks the runner is handed, and `run_scenario`,
    # which is how the runner is started -- a child of this shell on the cluster, where
    # something has to run after the runner is gone; an exec over it otherwise.
    # @@POST_RUN_BLOCK@@

    SCENARIO_FILE="${SCENARIO_FILE:-scenario.osc}"
    # Parameter file is a single-config scenario.config by default. On the
    # cluster robovast supplies the job's parameter document and sets
    # OUTPUT_RESULT_PER_SCENARIO=true so scenario_execution writes the run's
    # test.xml into the document's _output_dir subdirectory.
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
        run_scenario ${RUNNER_CMD} -o ${SCENARIO_OUTPUT_DIR} /config/${SCENARIO_FILE} ${POST_COMMAND_PARAM} --scenario-parameter-file ${SCENARIO_PARAMETER_FILE} ${PER_SCENARIO_PARAM} ${SIMULATION_PARAM} ${BT_LOG_PARAM} ${SCENARIO_EXECUTION_PARAMETERS}
    else
        log "Starting scenario execution (mode=${SCENARIO_MODE}) without config file..."
        run_scenario ${RUNNER_CMD} -o ${SCENARIO_OUTPUT_DIR} /config/${SCENARIO_FILE} ${POST_COMMAND_PARAM} ${SIMULATION_PARAM} ${BT_LOG_PARAM} ${SCENARIO_EXECUTION_PARAMETERS}
    fi
fi
