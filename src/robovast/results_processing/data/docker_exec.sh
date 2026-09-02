#!/usr/bin/env bash

# Default Docker image
DOCKER_IMAGE="ghcr.io/cps-test-lab/robovast:latest"
CONTAINER_NAME="ros2_exec_$$"

# Variable to track if cleanup has run
CLEANUP_DONE=0

# Cleanup function
cleanup() {
    if [ $CLEANUP_DONE -eq 1 ]; then
        return
    fi
    CLEANUP_DONE=1

    echo ""
    echo "Cleaning up container..."
    # Kill the container with timeout
    timeout 3 docker kill "$CONTAINER_NAME" 2>/dev/null || true
    # Force remove the container with timeout
    timeout 3 docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    # The staged copy of this directory, when there was one. Guarded on the variable and
    # not on the mode, so an interrupt before it was made removes nothing.
    if [ -n "${STAGED_SCRIPT_DIR:-}" ]; then
        rm -rf "$STAGED_SCRIPT_DIR"
    fi
}

# Set up signal handlers
trap 'cleanup; exit 130' SIGINT SIGTERM

# Show help
show_help() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS] SCRIPT [ARGS...]

Run a Python script with ROS from within a Docker container.

OPTIONS:
    --image IMAGE           Use a custom Docker image (default: ghcr.io/cps-test-lab/robovast:latest)
    --compat-version VER    Highest container protocol this host speaks
    --min-compat-version V  Lowest container protocol this host still supports
    --provenance-file PATH  Mount dirname(PATH) at /provenance in the container (for provenance JSON output)
    --cpus N                Cores the container may use, as a decimal count (e.g. 4, 2.5)
    --memory SIZE           Memory the container may use, in docker's spelling (e.g. 4g)
    -h, --help              Show this help message

EXAMPLE:
    $(basename "$0") my_script.py arg1 arg2
EOF
}

# Provenance mount (optional)
PROVENANCE_MOUNT=()
# What the container may use. Unset runs it uncontained, which is what a direct caller of this
# script gets; the postprocessing plugin always passes both, so that the local lane holds a
# conversion to the same figure the cluster lane reserves for it. The figure also reaches the
# conversion's worker count on its own: rosbags_process reads its cgroup quota rather than the
# machine's core count, so a capped container converts as many bags at once as it has cores.
LIMITS=()
COMPAT_VERSION=""
MIN_COMPAT_VERSION=""
COMPAT_LABEL="org.robovast.compat-version"

# Parse command-line arguments
while [ $# -gt 0 ]; do
    case $1 in
        -h|--help)
            show_help
            exit 0
            ;;
        --image)
            DOCKER_IMAGE="$2"
            shift 2
            ;;
        --compat-version)
            COMPAT_VERSION="$2"
            shift 2
            ;;
        --min-compat-version)
            MIN_COMPAT_VERSION="$2"
            shift 2
            ;;
        --cpus)
            if [ -z "${2:-}" ]; then
                echo "Error: --cpus requires a core count"
                exit 1
            fi
            LIMITS+=(--cpus "$2")
            shift 2
            ;;
        --memory)
            if [ -z "${2:-}" ]; then
                echo "Error: --memory requires a size"
                exit 1
            fi
            LIMITS+=(--memory "$2")
            shift 2
            ;;
        --provenance-file)
            if [ -z "${2:-}" ]; then
                echo "Error: --provenance-file requires a path"
                exit 1
            fi
            PROVENANCE_DIR="$(cd "$(dirname "$2")" && pwd)"
            PROVENANCE_MOUNT=(-v "$PROVENANCE_DIR:/provenance")
            shift 2
            ;;
        *)
            break
            ;;
    esac
done

# Check if script argument is provided
if [ $# -eq 0 ]; then
    echo "Error: No script specified"
    show_help
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# A service running as a sibling container hands every bind source below to the HOST's
# daemon, and this one is the robovast install itself -- a path that exists only inside the
# image. The daemon cannot find it, so it creates an empty directory and mounts that, and
# the container starts and reports "/scripts/ros2_exec.sh: No such file or directory".
# Staging the scripts under TMPDIR fixes it because TMPDIR is identity-mapped for exactly
# this reason (see container/service/docker-compose.yml, and robovast.service.sibling_paths):
# one absolute path, the same on both sides. The cluster lane solves the same problem one
# level up, with an initContainer copying them into an emptyDir.
if [ -n "${ROBOVAST_IN_CONTAINER:-}" ]; then
    STAGED_SCRIPT_DIR="$(mktemp -d -t robovast_scripts_XXXXXXXX)"
    cp -a "$SCRIPT_DIR/." "$STAGED_SCRIPT_DIR/"
    SCRIPT_DIR="$STAGED_SCRIPT_DIR"
fi
echo "Script directory: $SCRIPT_DIR"

# Extract the last argument (input folder path)
ARGS=("$@")
LAST_ARG="${ARGS[${#ARGS[@]}-1]}"

# Check if the last argument is a directory path
INPUT_MOUNT=()
CONTAINER_INPUT_PATH=""
if [ -d "$LAST_ARG" ]; then
    INPUT_DIR="$(cd "$LAST_ARG" && pwd)"
    CONTAINER_INPUT_PATH="/input"
    INPUT_MOUNT=(-v "$INPUT_DIR:$CONTAINER_INPUT_PATH")
    echo "Input directory: $INPUT_DIR"

    # Replace the last argument with the container path
    ARGS[${#ARGS[@]}-1]="$CONTAINER_INPUT_PATH"
elif [ -e "$LAST_ARG" ]; then
    echo "Error: Last argument '$LAST_ARG' exists but is not a directory"
    exit 1
fi

# Is there a Docker to ask at all? Checked BEFORE anything reads a label, because every
# question below returns an empty string when the daemon is missing and then blames the
# IMAGE for it.
#
# That misdirection cost two investigations. Run in a cluster pod, which has no Docker, this
# reported "cannot determine the container protocol version of
# 'ghcr.io/cps-test-lab/robovast:latest'" -- naming an upstream default nobody had chosen,
# on a deployment whose images live in a private registry. Both times the registry looked
# like the fault; both times the real one was that this script cannot run where it ran.
if ! docker info >/dev/null 2>&1; then
    echo "ERROR: this script needs a Docker daemon and cannot reach one."
    echo "  It runs a container directly, so it belongs on a machine with Docker -- not in"
    echo "  a cluster pod, where rosbag conversion runs as a Job instead."
    echo "  Reaching this from a campaign means the step was dispatched to the wrong lane:"
    echo "  the in-pod pass must skip what the Job already does"
    echo "  (results_processing.postprocessing.ROSBAG_JOB_NAMES)."
    exit 1
fi

# Container protocol check, off the image's label -- one `docker inspect`, nothing started.
if [ -n "$COMPAT_VERSION" ]; then
    MIN_COMPAT_VERSION="${MIN_COMPAT_VERSION:-$COMPAT_VERSION}"
    IMAGE_COMPAT=$(docker inspect --format "{{index .Config.Labels \"$COMPAT_LABEL\"}}" "$DOCKER_IMAGE" 2>/dev/null || echo "")
    COMPAT_SOURCE="label"
    if [ "$IMAGE_COMPAT" = "<no value>" ]; then
        IMAGE_COMPAT=""
    fi
    # A RANGE, not equality: postprocessing re-runs against the image a finished campaign
    # recorded, so equality made every protocol bump retroactively un-postprocess-able.
    if [ -z "$IMAGE_COMPAT" ]; then
        echo "ERROR: cannot determine the container protocol version of '$DOCKER_IMAGE'."
        echo "  This host speaks ${MIN_COMPAT_VERSION}..${COMPAT_VERSION}; the image carries"
        echo "  no $COMPAT_LABEL label, so it is either not a robovast image or predates it."
        exit 1
    elif [ "$IMAGE_COMPAT" -gt "$COMPAT_VERSION" ] || [ "$IMAGE_COMPAT" -lt "$MIN_COMPAT_VERSION" ]; then
        echo "ERROR: '$DOCKER_IMAGE' speaks container protocol $IMAGE_COMPAT (from its $COMPAT_SOURCE),"
        echo "  but this host speaks ${MIN_COMPAT_VERSION}..${COMPAT_VERSION}."
        if [ "$IMAGE_COMPAT" -gt "$COMPAT_VERSION" ]; then
            echo "  The image is NEWER than this robovast -- upgrade robovast."
        else
            echo "  Check out the robovast revision the campaign recorded"
            echo "  (_execution/execution.yaml: robovast_revision) and postprocess there."
        fi
        exit 1
    fi
fi

# Run the script inside the Docker container, calling ros2_exec.sh
# Pass arguments directly to avoid quote escaping issues
docker run \
    --name "$CONTAINER_NAME" \
    --rm \
    --user $(id -u):$(id -g) \
    -e PYTHONUNBUFFERED=1 \
    "${LIMITS[@]}" \
    -v "$SCRIPT_DIR:/scripts:ro" \
    "${INPUT_MOUNT[@]}" \
    "${PROVENANCE_MOUNT[@]}" \
    -w /scripts \
    "$DOCKER_IMAGE" \
    /scripts/ros2_exec.sh "${ARGS[@]}"

# Capture exit code and cleanup
EXIT_CODE=$?
cleanup
exit $EXIT_CODE
