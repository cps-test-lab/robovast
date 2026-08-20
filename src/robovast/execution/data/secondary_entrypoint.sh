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

# `_now` and `log`, shared verbatim with entrypoint.sh (see _LOG_BLOCK in
# robovast/common/execution.py). The node is plain `entrypoint` in both, not
# `entrypoint:${CONTAINER_NAME}`: which container spoke is already carried by this line's
# source file (`system_<name>.log` is what sets the run log's `container`) and by the live
# view's `name  | ` relay prefix, and a third copy inside the node field would list
# `entrypoint`, `entrypoint:sim`, `entrypoint:sut` in the filter as if they were different
# producers.
# @@LOG_BLOCK@@

# Fail fast if a required tool is missing instead of dying mid-startup. Before the redirect
# below, because that redirect is built out of `tee` and `stdbuf`: a missing one there would
# discard the very line that reports it. Written with a bare echo for the same reason.
for _tool in python3 stdbuf tee; do
    command -v "${_tool}" > /dev/null 2>&1 || {
        echo "ERROR: Required tool '${_tool}' not found in container image. Rebuild the image." >&2
        exit 1
    }
done

# Everything this script prints -- `log` lines and bare `echo`s alike -- lands in the durable
# artifact from here on. Previously only `log` lines were teed and the redirect sat further
# down, so anything echoed before it reached the live log and never the file.
#
# `stdbuf -oL` unbuffers tee so the log panel sees lines as they are printed.
exec > >(stdbuf -oL tee -a "${LOG_FILE}")
exec 2>&1

log "Secondary container starting ($(hostname))..."
log "Running as UID: $(id -u), GID: $(id -g)..."
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

# `stdbuf -oL` on the redirect above unbuffers TEE, which is not where the buffering is: the workload's
# stdout is now a pipe, so libc block-buffers it at the source in 4-8 KB chunks and tee
# cannot flush what it was never given. The simulator's log panel then goes quiet for a
# minute and dumps a wall of text -- which is the difference between watching a run and
# reading its transcript afterwards.
#
# The main container is only spared this by accident: PYTHONUNBUFFERED is set in the
# RoboVAST image's Dockerfile. A SIDECAR is explicitly allowed to be a vanilla image --
# that is the whole claim of the ROS shape, "point it at any nav2 image and it works" --
# so it cannot inherit anything, and the promise that its image can be stock is exactly
# what breaks its liveness. Hence: state it here, for whatever image runs.
export PYTHONUNBUFFERED=1

SOCKET="/ipc/${CONTAINER_NAME}"

# Which distributions this container holds -- ONLY that, not the pod's host facts, which the
# main container already recorded and which are the same pod. The packages are what differ, and
# in the ROS shape they differ where it matters most: the simulator runs here, so every asset
# provider a campaign used is installed in this container and in no other. `|| true` because a
# record about a run must never be the reason the run fails, and an older image may not mount
# the script at all.
python3 /config/collect_sysinfo.py --no-sysinfo \
  --distributions "${OUTPUT_DIR}/distributions_${CONTAINER_NAME}.json" || true

# Start resource monitor
python3 /config/monitor_resources.py "${OUTPUT_DIR}/resource_usage_${CONTAINER_NAME}.csv" &
_monitor_pid=$!
log "Started resource monitor (PID=${_monitor_pid}) -> ${OUTPUT_DIR}/resource_usage_${CONTAINER_NAME}.csv"

# Everything a sidecar produces is written into /out, and /out is an emptyDir that dies
# with the pod. The only thing that copies it out is the main container's --post-run
# upload -- which runs while the scenario is finishing, i.e. BEFORE kubelet stops a
# sidecar. So anything a sidecar wrote after that moment was simply lost: the simulator's
# run.npz and capture/ (an .npz writes its zip index at close, so it exists only at
# shutdown) never reached the store, and every sidecar log was truncated to whatever it
# had emitted by upload time -- the simulator's to nine lines of start-up for a 99-second
# run. Locally none of this shows, because /out is a bind mount onto the host and a late
# write just lands; on the cluster the artifact is silently absent.
#
# So a sidecar gets the same post-run treatment the main container has: run the workload
# as a child rather than exec'ing over this shell, wait for it, and then upload. mc mirror
# is incremental, so re-walking /out costs little and picks up exactly what is new.
#
# The uploaders do not race for a given file's CONTENT -- each container writes its own
# log and its own resource CSV, and uploads only after both have stopped -- but they do
# all mirror the same shared /out to the same prefix, so they collide on objects the
# earlier uploader already put there. That collision is resolved by --overwrite in
# s3_upload.sh (see the comment there); without it the store keeps whichever truncated
# copy landed first. Do not "fix" this by having sidecars exclude the shared paths: the
# main container's own log is still growing when it uploads, so a later, complete
# overwrite is exactly what makes the archived logs usable.
_post_run() {
    # The monitor first, so its CSV is complete before anything copies it.
    if [ -n "${_monitor_pid}" ] && kill -0 "${_monitor_pid}" 2>/dev/null; then
        kill -TERM "${_monitor_pid}" 2>/dev/null || true
        wait "${_monitor_pid}" 2>/dev/null || true
    fi
    # Written by the main container's entrypoint into the SHARED /tmp. Absent on the
    # local lane, where /out is a bind mount and there is nothing to upload -- so its
    # absence is the normal case there, not a failure.
    if [ -x /tmp/s3_upload.sh ]; then
        log "Uploading ${CONTAINER_NAME} artifacts left after the scenario finished..."
        /tmp/s3_upload.sh || log "WARNING: ${CONTAINER_NAME} post-run upload failed"
    fi
}

# Run the workload as a child and forward SIGTERM, so it shuts down the way it would have
# as PID 1 -- `roqsim sim` traps it to flush its recording, and a hard kill would leave the
# .npz without its index. `wait` returns >128 when a trapped signal interrupts it, hence
# the loop: the second wait is the one that reaps.
_child=""
_forward() { [ -n "${_child}" ] && kill -TERM "${_child}" 2>/dev/null; return 0; }
trap _forward TERM INT

run_child() {
    # Line-buffered, for the same reason as PYTHONUNBUFFERED above but for the half of a
    # ROS stack that is not Python: stdbuf's LD_PRELOAD is inherited, so a `ros2 launch`
    # here reaches the C++ nodes it spawns. A binary that ignores it (static, setuid) is
    # simply unaffected -- this can make output more live, never less.
    stdbuf -oL -eL "$@" &
    _child=$!
    # `|| _rc=$?` and not a bare `wait`: this script runs under `set -e`, and a `wait`
    # interrupted by a trapped signal returns 128+signo. A bare one therefore exits the
    # shell the instant SIGTERM arrives -- skipping the flush AND the upload, which is
    # precisely the failure this function exists to prevent, and it looks identical to
    # having no trap at all.
    _rc=0
    wait "${_child}" || _rc=$?
    # The first wait returns when the signal is handled, not when the child is gone; the
    # loop is what reaps it, so the workload gets to finish writing before we upload.
    while kill -0 "${_child}" 2>/dev/null; do
        wait "${_child}" || _rc=$?
    done
    _post_run
    exit "${_rc}"
}

# A container that declares its own command runs THAT, with everything above already
# done for it: the ROS overlay sourced, stdout teed into the job's log directory, and
# the resource monitor running. Exec'ing the command directly as the container's
# entrypoint -- which is what used to happen -- skipped all three. The ROS one is not a
# nicety: a colcon package like the MuJoCo bridge only reaches PYTHONPATH once
# /opt/ros and /ws/install are sourced, so `roqsim sim --ros` died instantly with
# "unknown plugin 'ros2_bridge'" while the scenario waited out its /scan timeout with
# no log anywhere to say why. Any simulator backend would have hit the same wall, so
# this belongs here and not in one backend's command string.
if [ -n "${ROBOVAST_CONTAINER_COMMAND}" ]; then
    log "Starting container command: ${ROBOVAST_CONTAINER_COMMAND}"
    # Unquoted on purpose: the command arrives as one string and has to word-split.
    run_child ${ROBOVAST_CONTAINER_COMMAND}
fi

if command -v ros2 > /dev/null 2>&1; then
    log "Starting scenario-execution-server-ros on socket '${SOCKET}'..."
    run_child ros2 run scenario_execution_server_ros scenario_execution_server_ros --watchdog ${WATCHDOG_TIMEOUT} --connect-timeout ${CONNECT_TIMEOUT} --socket "${SOCKET}"
else
    log "Starting scenario-execution-server on socket '${SOCKET}'..."
    run_child scenario_execution_server --watchdog ${WATCHDOG_TIMEOUT} --connect-timeout ${CONNECT_TIMEOUT} --socket "${SOCKET}"
fi
