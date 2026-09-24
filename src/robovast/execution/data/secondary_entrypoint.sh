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

# The sockets the scenario drives the sidecars over, and a per-pod scratch space: an emptyDir on
# the cluster, so what one container instance leaves here the next
# instance in the SAME pod finds and the next job never does.
IPC_DIR="${IPC_DIR:-/ipc}"

# A second instance of this container starts NOTHING.
#
# A workload container that dies is restarted by the kubelet -- `restartPolicy: Always` is the
# only policy a native sidecar may carry, and the sidecar shape is what starts the simulator
# before the scenario and ends the pod with it. So the restart itself cannot be refused. What can
# be refused is the workload: a simulator brought back mid-trial starts a fresh world under a
# stack that is still running the old one, and every result from that moment is about the
# restart and not about the trial. The trial ended when the first instance died. This instance
# says so, keeps the evidence the dead one left in /out, and waits to be ended: the runner reads
# the restart off the pod and deletes the job, recording the run as invalid with what the
# container died of.
#
# It HOLDS rather than exiting, and that is forensic rather than cosmetic. The runner reads what
# the container died of from the pod's `last_state.terminated`, which names the first instance's
# end (OOMKilled, exit 137) for exactly as long as this instance keeps running. An exit here would
# be followed by another kubelet restart, after which that field names this script's own exit
# and the record says the guard died, not the simulator.
_STARTED_MARKER="${IPC_DIR}/.${CONTAINER_NAME}.started"
# The marker the pod's uploader waits for before it delivers /out: this container has
# finished writing. Its files are complete once its workload and its monitor have stopped
# -- and once it has been restarted, since the instance that wrote them is gone.
_DONE_MARKER="${IPC_DIR}/done.${CONTAINER_NAME}"
if [ -e "${_STARTED_MARKER}" ]; then
    log "ERROR: ${CONTAINER_NAME} is a restarted container: an earlier instance started at $(cat "${_STARTED_MARKER}") and died, and the trial died with it. Not starting the workload again; waiting for the runner to end this job."
    touch "${_DONE_MARKER}"
    trap 'exit 0' TERM INT
    while true; do
        sleep 3600 &
        wait $! || true
    done
fi
date -u +%Y-%m-%dT%H:%M:%SZ > "${_STARTED_MARKER}"
# Set up the ROS overlay first (when present) so the ROS server runner
# (scenario_execution_server_ros / ros2) is on PATH for the check below: it only
# lands there once the ROS overlay and the /ws workspace are sourced, so checking
# earlier would spuriously report "no scenario-execution server" on a ROS image.
ROS_SETUP_ANNOUNCE=log
# @@ROS_SETUP_BLOCK@@

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

SOCKET="${IPC_DIR}/${CONTAINER_NAME}"

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
# with the pod. The pod's uploader container delivers it once every container has said it
# is finished writing, and this container says so with its done marker: after its workload
# has exited and its resource monitor has stopped, so the CSV is complete before anything
# reads it. The simulator's run.npz and capture/ exist only at shutdown (an .npz writes its
# zip index at close), which is why the workload is stopped, and reaped, before the marker
# is written rather than left running until the kubelet's TERM.
#
# The workload is stopped when the SCENARIO says it is finished, by the main container's
# own marker; where it is never written (a bind-mounted /out), a sidecar simply holds.
_DONE_MAIN="${IPC_DIR}/done.main"

_post_run() {
    # The monitor first, so its CSV is complete before the marker says it is.
    if [ -n "${_monitor_pid}" ] && kill -0 "${_monitor_pid}" 2>/dev/null; then
        kill -TERM "${_monitor_pid}" 2>/dev/null || true
        wait "${_monitor_pid}" 2>/dev/null || true
    fi
    touch "${_DONE_MARKER}"
    log "${CONTAINER_NAME} has finished writing; wrote ${_DONE_MARKER}"
}

# Run the workload as a child and forward SIGTERM, so it shuts down the way it would have
# as PID 1 -- `roqsim sim` traps it to flush its recording, and a hard kill would leave the
# .npz without its index. `wait` returns >128 when a trapped signal interrupts it, hence
# the loop: the second wait is the one that reaps.
_child=""
_sleeper=""
_terminating=""
# `|| true` on each kill: this runs under `set -e`, and a signal that arrives once the
# workload is gone would otherwise end the shell inside its own trap, before it has stopped
# the sleeper or written its marker.
_forward() {
    _terminating=1
    if [ -n "${_child}" ]; then kill -TERM "${_child}" 2>/dev/null || true; fi
    if [ -n "${_sleeper}" ]; then kill "${_sleeper}" 2>/dev/null || true; fi
    return 0
}
trap _forward TERM INT

# An interruptible pause: `sleep &` + `wait` and not a bare `sleep`, because only `wait`
# is interruptible by the trap above, so a bare sleep would delay teardown by up to its own
# duration. The trap also ends the sleeper, and the flag is read again once the sleeper is
# known: a TERM that lands between the flag being read and `wait` would otherwise be
# noticed only when the sleep ended. The sleeper is ended with the wait, so an interrupted
# one does not outlive the pause holding the log's pipe open.
_pause() {
    sleep "${1:-1}" &
    _sleeper=$!
    if [ -n "${_terminating}" ]; then
        kill "${_sleeper}" 2>/dev/null || true
        return 0
    fi
    wait "${_sleeper}" 2>/dev/null || true
    kill "${_sleeper}" 2>/dev/null || true
}

# Stay alive until the pod is actually being torn down.
#
# A native sidecar that EXITS is RESTARTED by the kubelet -- that is what `restartPolicy: Always`
# means, and it is not optional for a container that must start before the scenario. So a workload
# finishing early does not end the container's job; exiting would hand RoboVAST a restart, and a
# restarted container invalidates the trial (`pod_restarted_containers`) whatever it died of.
#
# That is not hypothetical: the scenario-execution server exits cleanly the moment its client goes
# away, which happens as the scenario ENDS -- while the main container is still finishing. So a
# perfectly good run was failed by its own teardown order, every time, and the exit code said
# `Completed (exit 0)` because nothing had gone wrong.
#
# Holding also keeps `_post_run` where it belongs. Reached early it kills this container's resource
# monitor mid-run, so the CSV stops at the moment the workload happened to finish rather than at
# the end of the trial: the monitor runs until the scenario's own marker says the trial is over.
_hold_for_scenario() {
    log "${CONTAINER_NAME} workload finished cleanly; holding so the kubelet does not restart it"
    while [ -z "${_terminating}" ] && [ ! -e "${_DONE_MAIN}" ]; do
        _pause 1
    done
}

_hold_for_teardown() {
    while [ -z "${_terminating}" ]; do
        _pause 3600
    done
}

run_child() {
    # Line-buffered, for the same reason as PYTHONUNBUFFERED above but for the half of a
    # ROS stack that is not Python: stdbuf's LD_PRELOAD is inherited, so a `ros2 launch`
    # here reaches the C++ nodes it spawns. A binary that ignores it (static, setuid) is
    # simply unaffected -- this can make output more live, never less.
    stdbuf -oL -eL "$@" &
    _child=$!
    # Watch the workload and the scenario together: a simulator never exits on its own, so
    # the scenario's marker is what ends it -- a TERM, which `roqsim sim` traps to flush its
    # recording. The workload's own exit is seen within a poll.
    #
    # `|| _rc=$?` and not a bare `wait`: this script runs under `set -e`, and a `wait`
    # interrupted by a trapped signal returns 128+signo. A bare one therefore exits the
    # shell the instant SIGTERM arrives -- skipping the flush AND the marker, which is
    # precisely the failure this function exists to prevent, and it looks identical to
    # having no trap at all.
    _rc=0
    _stopped_by_scenario=""
    while kill -0 "${_child}" 2>/dev/null; do
        if [ -z "${_stopped_by_scenario}" ] && [ -e "${_DONE_MAIN}" ]; then
            _stopped_by_scenario=1
            log "The scenario has finished; stopping the ${CONTAINER_NAME} workload"
            kill -TERM "${_child}" 2>/dev/null || true
        fi
        _pause 1
    done
    # The pause returns when a signal is handled, not when the child is gone; this is what
    # reaps it, so the workload gets to finish writing before the marker is written.
    wait "${_child}" || _rc=$?
    # A workload this script stopped ended the way it was asked to, whatever status a TERM
    # gave it. A clean finish while the pod runs on is a container with nothing left to do,
    # not one that is allowed to leave. A FAILURE still exits: the restart the kubelet then
    # performs is a true signal, the instance it starts refuses the workload (the marker
    # check at the top), and invalidating that trial is the right outcome.
    if [ -n "${_stopped_by_scenario}" ]; then
        _rc=0
    elif [ "${_rc}" -eq 0 ] && [ -z "${_terminating}" ]; then
        _hold_for_scenario
    fi
    _post_run
    if [ "${_rc}" -eq 0 ]; then
        _hold_for_teardown
    fi
    exit "${_rc}"
}

# A container that declares its own command runs THAT, with everything above already
# done for it: the ROS overlay sourced, stdout teed into the job's log directory, and
# the resource monitor running. Exec'ing the command directly as the container's
# entrypoint skips all three. The ROS one is not a nicety: a colcon package like the MuJoCo bridge only reaches PYTHONPATH once
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
