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
}

# Set up signal handlers
trap 'cleanup; exit 130' SIGINT SIGTERM

# Show help
show_help() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS] SCRIPT [ARGS...]

Run a Python script with ROS from within a Docker container.

OPTIONS:
    --image IMAGE       Use a custom Docker image (default: ghcr.io/cps-test-lab/robovast:latest)
    -h, --help          Show this help message

EXAMPLE:
    $(basename "$0") my_script.py arg1 arg2
EOF
}

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
echo "Script directory: $SCRIPT_DIR"

# Extract the last argument (input folder path)
ARGS=("$@")
LAST_ARG="${ARGS[${#ARGS[@]}-1]}"

# Check if the last argument is a directory path
INPUT_MOUNT=""
CONTAINER_INPUT_PATH=""
if [ -d "$LAST_ARG" ]; then
    INPUT_DIR="$(cd "$LAST_ARG" && pwd)"
    CONTAINER_INPUT_PATH="/input"
    INPUT_MOUNT="-v $INPUT_DIR:$CONTAINER_INPUT_PATH"
    echo "Input directory: $INPUT_DIR"

    # Replace the last argument with the container path
    ARGS[${#ARGS[@]}-1]="$CONTAINER_INPUT_PATH"
elif [ -e "$LAST_ARG" ]; then
    echo "Error: Last argument '$LAST_ARG' exists but is not a directory"
    exit 1
fi

# Build the command string with proper escaping
CMD_ARGS=""
for arg in "${ARGS[@]}"; do
    # Escape single quotes in the argument and wrap in single quotes
    escaped_arg="${arg//\'/\'\\\'\'}"
    CMD_ARGS="$CMD_ARGS '$escaped_arg'"
done

# Run the script inside the Docker container
docker run \
    --name "$CONTAINER_NAME" \
    --rm \
    --user $(id -u):$(id -g) \
    -v "$SCRIPT_DIR:/scripts:ro" \
    $INPUT_MOUNT \
    -w /scripts \
    "$DOCKER_IMAGE" \
    bash -c "
        # Detect ROS distro by checking /opt/ros/<distro> directories
        ros_distros=\$(ls /opt/ros 2>/dev/null)
        if [ -z \"\$ros_distros\" ]; then
            echo 'Error: No ROS distributions found in /opt/ros.'
            exit 1
        fi

        # Use the first found distro
        ROS_DISTRO=\$(echo \"\$ros_distros\" | head -n 1)
        echo \"Detected ROS distribution: \$ROS_DISTRO\"

        source \"/opt/ros/\$ROS_DISTRO/setup.bash\"
        exec python3 $CMD_ARGS
    "

# Capture exit code and cleanup
EXIT_CODE=$?
cleanup
exit $EXIT_CODE