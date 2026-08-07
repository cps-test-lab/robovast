#!/bin/bash -e
# Build the robovast container image(s).
#
# Two images, and only the second knows what a simulator is:
#
#   robovast       the framework image: ROS, nav2, scenario-execution, the RoboVAST
#                  contract. Simulator-agnostic, and what every campaign without a
#                  stepped simulator runs.
#   robosito       that image plus robosito, for a **stepped** run where the simulator
#                  shares the scenario's process (see Dockerfile.robosito). A ROS
#                  campaign does not need it: there the simulator runs in its own
#                  container from robosito's own image.
#
# Mirrors robosito/container/build.sh, which mirrors this one -- same --image / --project
# / --ros-distro / --push handling, so the two repos' builders stay learnable as one.
#
# Usage:
#   ./container/robovast/build.sh [--image robovast|robosito|all] [--project <prefix>] \
#                                 [--ros-distro <distro>] [--push] [-- <extra docker build args>]

BASEDIR=$(cd "$(dirname "$0")" && pwd)
ROS_DISTRO="jazzy"
PROJECT=""
IMAGE="robovast"
ROBOSITO_SRC=""
ROBOSITO_REPO="${ROBOSITO_REPO:-https://github.com/cps-test-lab/robosito.git}"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --image)
      IMAGE="$2"
      shift 2
      ;;
    --project)
      PROJECT="$2"
      shift 2
      ;;
    --robosito-src)
      ROBOSITO_SRC="$2"
      shift 2
      ;;
    --ros-distro)
      ROS_DISTRO="$2"
      shift 2
      ;;
    --push|-n)
      PUSH=1
      shift
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done

# Pass remaining arguments to docker build
EXTRA_ARGS="$@"

case "$IMAGE" in
  robovast|robosito|all) ;;
  *) echo "unknown --image '$IMAGE' (expected robovast, robosito or all)" >&2; exit 2 ;;
esac

echo "Using Dockerfile: $BASEDIR"
echo "From Context: $PWD"
echo "Project: $PROJECT"
echo "Image(s): $IMAGE"

# ensure PROJECT ends with a slash when non-empty
if [[ -n "${PROJECT}" ]]; then
  [[ "${PROJECT}" == */ ]] || PROJECT="${PROJECT}/"
fi

build_base() {
  DOCKER_BUILDKIT=1 docker build \
    --build-arg ROS_DISTRO=$ROS_DISTRO \
    $EXTRA_ARGS \
    -t robovast_${ROS_DISTRO}:latest \
    -f $BASEDIR/Dockerfile \
    $PWD

  docker tag robovast_${ROS_DISTRO} ${PROJECT}robovast_${ROS_DISTRO}

  if [ -n "${PUSH:-}" ]; then
    echo "Pushing docker image to ${PROJECT}robovast_${ROS_DISTRO}"
    docker push "${PROJECT}robovast_${ROS_DISTRO}"
  fi
}

build_robosito() {
  # FROM the base by its *resolved* tag rather than a floating one, so the derived image
  # is always built on the base that was just built (or the one explicitly named) and
  # the two cannot drift. It inherits /etc/robovast_compat_version from there, which is
  # why the compat check needs no second home.
  local base="${BASE_IMAGE:-${PROJECT}robovast_${ROS_DISTRO}}"

  # Stage the source into a context of its own. Two reasons it is not built against the
  # repo root: the clone case has no source there at all, and the local case would
  # otherwise ship the whole surrounding tree (results dirs included) to the daemon.
  local ctx
  ctx=$(mktemp -d) || return 1
  trap 'rm -rf "$ctx"' RETURN

  if [[ -n "$ROBOSITO_SRC" ]]; then
    echo "robosito source: $ROBOSITO_SRC (local checkout)"
    # -a to keep symlinks and modes; the excludes are what must never reach an image --
    # a .git of several hundred MB, a host venv whose binaries are wrong for the image,
    # and caches that only invalidate layers.
    # 'external' is vendored upstream source (unitree_ros and friends, ~400 MB) that none
    # of the packages installed below reads -- the meshes they need are inside the rst_*
    # packages themselves. Staging it doubled the context for nothing.
    rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
          --exclude='*.egg-info' --exclude='build/' --exclude='install/' --exclude='log/' \
          --exclude='external/' --exclude='docs/build/' \
          "${ROBOSITO_SRC%/}/" "$ctx/robosito/" || return 1
  else
    echo "robosito source: ${ROBOSITO_REPO} @ ${ROBOSITO_REF:-main} (clone)"
    git clone --quiet "$ROBOSITO_REPO" "$ctx/robosito" || return 1
    git -C "$ctx/robosito" checkout --quiet "${ROBOSITO_REF:-main}" || return 1
  fi

  DOCKER_BUILDKIT=1 docker build \
    --build-arg BASE_IMAGE="$base" \
    $EXTRA_ARGS \
    -t robovast_robosito_${ROS_DISTRO}:latest \
    -f $BASEDIR/Dockerfile.robosito \
    "$ctx"

  docker tag robovast_robosito_${ROS_DISTRO} ${PROJECT}robovast_robosito_${ROS_DISTRO}

  if [ -n "${PUSH:-}" ]; then
    echo "Pushing docker image to ${PROJECT}robovast_robosito_${ROS_DISTRO}"
    docker push "${PROJECT}robovast_robosito_${ROS_DISTRO}"
  fi
}

case "$IMAGE" in
  robovast) build_base ;;
  robosito) build_robosito ;;
  all)      build_base && build_robosito ;;
esac
