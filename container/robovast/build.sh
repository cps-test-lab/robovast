#!/bin/bash -e
# Build the robovast container image(s).
#
# Two images, and only the second knows what a simulator is:
#
#   robovast       the framework image: ROS, nav2, scenario-execution, the RoboVAST
#                  contract. Simulator-agnostic, and what every campaign without a
#                  stepped simulator runs.
#   roqsim       that image plus roqsim, for a **stepped** run where the simulator
#                  shares the scenario's process (see Dockerfile.roqsim). A ROS
#                  campaign does not need it: there the simulator runs in its own
#                  container from roqsim's own image.
#
# Mirrors roqsim/container/build.sh, which mirrors this one -- same --image / --project
# / --ros-distro / --push handling, so the two repos' builders stay learnable as one.
#
# Usage:
#   ./container/robovast/build.sh [--image robovast|roqsim|all] [--project <prefix>] \
#                                 [--ros-distro <distro>] [--push] \
#                                 [--roqsim-src <path>] [--scenario-execution-src <path>] \
#                                 [-- <extra docker build args>]
#
# The two --*-src flags are the development hatch: each repo is otherwise cloned at a pin,
# which cannot build a commit that is not pushed yet. A build that used one says so in its
# log, because the resulting image no longer corresponds to the pin.

BASEDIR=$(cd "$(dirname "$0")" && pwd)
ROS_DISTRO="jazzy"
PROJECT=""
IMAGE="robovast"
ROQSIM_SRC=""
SCENARIO_EXECUTION_SRC=""
ROQSIM_REPO="${ROQSIM_REPO:-https://github.com/cps-test-lab/roqsim.git}"
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
    --roqsim-src)
      ROQSIM_SRC="$2"
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
  robovast|roqsim|all) ;;
  *) echo "unknown --image '$IMAGE' (expected robovast, roqsim or all)" >&2; exit 2 ;;
esac

# A push from here publishes an *interim* image for the cluster, so it targets
# CLUSTER_PLATFORM -- not the host's architecture (an arm64 Mac would otherwise push
# an image no node can run, failing as an exec-format error in a pod rather than
# here), and not the full multi-arch set either: emulating an arm64 ROS build under
# QEMU takes hours to produce a variant nothing pulls. Publishing every architecture
# is CI's job, from container/platforms.env. Pass --platform to override.
# Building without --push targets this machine, so the result is runnable locally.
# `docker buildx build` replaces `docker build` so local and CI use one builder.
# $1 platform, $2 local tag, $3 published tag. Sets BUILDX_ARGS including the -t flags.
#
# The tags differ by mode, and that is the point: `docker buildx build --push` publishes
# *every* -t it is given, so tagging with the bare local name as well would try to push
# `library/robovast_jazzy` to Docker Hub and fail with "push access denied". The old
# `docker build` + `docker tag` + `docker push <prefixed>` sequence pushed only the
# prefixed one, and that behaviour has to be preserved deliberately here.
buildx_args() {
  local platform="$1" local_tag="$2" published_tag="$3"
  BUILDX_ARGS=()
  [[ -n "$platform" ]] && BUILDX_ARGS+=(--platform "$platform")
  if [[ -n "${PUSH:-}" ]]; then
    BUILDX_ARGS+=(--push -t "$published_tag")
  elif [[ "$platform" == *,* ]]; then
    # No single image exists to load for a multi-platform build.
    echo "refusing to build $platform without --push: a multi-platform image cannot be loaded into the local docker daemon" >&2
    return 2
  else
    # Local build: the bare name is what a developer runs, and the prefixed one is
    # what a later --push would publish, so both are useful in the daemon.
    BUILDX_ARGS+=(--load -t "$local_tag")
    [[ -n "$published_tag" && "$published_tag" != "$local_tag" ]] \
      && BUILDX_ARGS+=(-t "$published_tag")
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

  buildx_args "${PLATFORM:-${PUSH:+$CLUSTER_PLATFORM}}" \
    "robovast_${ROS_DISTRO}:latest" "${PROJECT}robovast_${ROS_DISTRO}" || return $?

  docker buildx build \
    "${BUILDX_ARGS[@]}" \
    --build-arg ROS_DISTRO=$ROS_DISTRO \
    $EXTRA_ARGS \
    -f $BASEDIR/Dockerfile \
    "$ctx"
}

build_roqsim() {
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

  if [[ -n "$ROQSIM_SRC" ]]; then
    echo "roqsim source: $ROQSIM_SRC (local checkout)"
    # -a to keep symlinks and modes; the excludes are what must never reach an image --
    # a .git of several hundred MB, a host venv whose binaries are wrong for the image,
    # and caches that only invalidate layers.
    # 'external' is vendored upstream source (unitree_ros and friends, ~400 MB) that none
    # of the packages installed below reads -- the meshes they need are inside the rst_*
    # packages themselves. Staging it doubled the context for nothing.
    rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
          --exclude='*.egg-info' --exclude='build/' --exclude='install/' --exclude='log/' \
          --exclude='external/' --exclude='docs/build/' \
          "${ROQSIM_SRC%/}/" "$ctx/roqsim/" || return 1
  else
    # main was restructured to a sim_suite_* layout for a different consumer and no longer
    # has the rst_* packages installed below; robovast is the branch that still does.
    echo "roqsim source: ${ROQSIM_REPO} @ ${ROQSIM_REF:-robovast} (clone)"
    git clone --quiet "$ROQSIM_REPO" "$ctx/roqsim" || return 1
    git -C "$ctx/roqsim" checkout --quiet "${ROQSIM_REF:-robovast}" || return 1
  fi

  buildx_args "${PLATFORM:-${PUSH:+$CLUSTER_PLATFORM}}" \
    "robovast_roqsim_${ROS_DISTRO}:latest" "${PROJECT}robovast_roqsim_${ROS_DISTRO}" \
    || return $?

  docker buildx build \
    "${BUILDX_ARGS[@]}" \
    --build-arg BASE_IMAGE="$base" \
    $EXTRA_ARGS \
    -f $BASEDIR/Dockerfile.roqsim \
    "$ctx"
}

case "$IMAGE" in
  robovast) build_base ;;
  roqsim) build_roqsim ;;
  all)      build_base && build_roqsim ;;
esac
