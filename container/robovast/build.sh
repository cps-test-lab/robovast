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
#                                 [--ros-distro <distro>] [--push] \
#                                 [--robosito-src <path>] [--scenario-execution-src <path>] \
#                                 [-- <extra docker build args>]
#
# The two --*-src flags are the development hatch: each repo is otherwise cloned at a pin,
# which cannot build a commit that is not pushed yet. A build that used one says so in its
# log, because the resulting image no longer corresponds to the pin.

BASEDIR=$(cd "$(dirname "$0")" && pwd)
ROS_DISTRO="jazzy"
PROJECT=""
IMAGE="robovast"
ROBOSITO_SRC=""
SCENARIO_EXECUTION_SRC=""
ROBOSITO_REPO="${ROBOSITO_REPO:-https://github.com/cps-test-lab/robosito.git}"
PLATFORM=""

# shellcheck source=../platforms.env
. "$BASEDIR/../platforms.env"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --image)
      IMAGE="$2"
      shift 2
      ;;
    --platform)
      PLATFORM="$2"
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
    --scenario-execution-src)
      SCENARIO_EXECUTION_SRC="$2"
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

# A push publishes for the cluster (linux/amd64), so honour the declared policy rather
# than the host's architecture -- an arm64 Mac otherwise pushes an image no node can
# run, failing as an exec-format error in a pod rather than here. Building without
# --push targets this machine, which is what makes the result runnable locally.
# `docker buildx build` replaces `docker build` so local and CI use one builder.
buildx_args() {
  local platform="$1"
  BUILDX_ARGS=()
  [[ -n "$platform" ]] && BUILDX_ARGS+=(--platform "$platform")
  if [[ -n "${PUSH:-}" ]]; then
    BUILDX_ARGS+=(--push)
  elif [[ "$platform" == *,* ]]; then
    # No single image exists to load for a multi-platform build.
    echo "refusing to build $platform without --push: a multi-platform image cannot be loaded into the local docker daemon" >&2
    return 2
  else
    BUILDX_ARGS+=(--load)
  fi
}

echo "Using Dockerfile: $BASEDIR"
echo "From Context: $PWD"
echo "Project: $PROJECT"
echo "Image(s): $IMAGE"

# ensure PROJECT ends with a slash when non-empty
if [[ -n "${PROJECT}" ]]; then
  [[ "${PROJECT}" == */ ]] || PROJECT="${PROJECT}/"
fi

build_base() {
  # A context of its own, holding only what the Dockerfile stages. The repo root was passed
  # before and never read -- no COPY in that Dockerfile touched it -- so this only stops the
  # whole working tree (frontend/ui/node_modules included) being sent to the daemon on every build.
  local ctx
  ctx=$(mktemp -d) || return 1
  trap 'rm -rf "$ctx"' RETURN
  mkdir -p "$ctx/scenario-execution-src"

  if [[ -n "$SCENARIO_EXECUTION_SRC" ]]; then
    echo "scenario-execution source: $SCENARIO_EXECUTION_SRC (local checkout)"
    # The colcon artifacts must not travel: build/ and install/ from the host are wrong for
    # the image and would be picked up ahead of what colcon builds inside it.
    rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
          --exclude='*.egg-info' --exclude='build/' --exclude='install/' --exclude='log/' \
          "${SCENARIO_EXECUTION_SRC%/}/" "$ctx/scenario-execution-src/" || return 1
  else
    echo "scenario-execution source: pinned clone (see Dockerfile)"
  fi

  buildx_args "${PLATFORM:-${PUSH:+$PLATFORMS_ROBOVAST}}" || return $?

  # One buildx invocation tagged with both names: a multi-platform build produces no
  # local image for `docker tag` to rename afterwards.
  docker buildx build \
    "${BUILDX_ARGS[@]}" \
    --build-arg ROS_DISTRO=$ROS_DISTRO \
    $EXTRA_ARGS \
    -t robovast_${ROS_DISTRO}:latest \
    -t ${PROJECT}robovast_${ROS_DISTRO} \
    -f $BASEDIR/Dockerfile \
    "$ctx"
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
    # main was restructured to a sim_suite_* layout for a different consumer and no longer
    # has the rst_* packages installed below; robovast is the branch that still does.
    echo "robosito source: ${ROBOSITO_REPO} @ ${ROBOSITO_REF:-robovast} (clone)"
    git clone --quiet "$ROBOSITO_REPO" "$ctx/robosito" || return 1
    git -C "$ctx/robosito" checkout --quiet "${ROBOSITO_REF:-robovast}" || return 1
  fi

  buildx_args "${PLATFORM:-${PUSH:+$PLATFORMS_ROBOSITO}}" || return $?

  docker buildx build \
    "${BUILDX_ARGS[@]}" \
    --build-arg BASE_IMAGE="$base" \
    $EXTRA_ARGS \
    -t robovast_robosito_${ROS_DISTRO}:latest \
    -t ${PROJECT}robovast_robosito_${ROS_DISTRO} \
    -f $BASEDIR/Dockerfile.robosito \
    "$ctx"
}

case "$IMAGE" in
  robovast) build_base ;;
  robosito) build_robosito ;;
  all)      build_base && build_robosito ;;
esac
