#!/bin/bash -e
# Build the robovast container image(s).
#
# Two images, and only the second knows what a simulator is:
#
#   robovast         the framework image: ROS, nav2, scenario-execution, the RoboVAST
#                    contract. Simulator-agnostic, and what every campaign without a
#                    simulator runs.
#   robovast-roqsim  that image plus roqsim. Used by **both** roqsim shapes: it is the
#                    only image carrying roqsim *and* the RoboVAST contract, so the ROS
#                    shape runs its own simulator container from this image too.
#
# The names are the family members robovast.common.execution.FAMILY_MEMBERS resolves
# ``family:<member>`` to, and the repositories .github/workflows/image.yml publishes. They
# used to carry the ROS distro (``robovast_jazzy``), which meant a local build produced
# repositories no default, doc or .vast ever named; the distro is a --tag now.
#
# Usage:
#   ./container/robovast/build.sh [--image robovast|roqsim|all] [--project <prefix>] \
#                                 [--tag <tag>] [--ros-distro <distro>] [--push] \
#                                 [--roqsim-src <path>] [--scenario-execution-src <path>] \
#                                 [-- <extra docker build args>]
#
# The two --*-src flags are the development hatch: each repo is otherwise cloned BY THE
# DOCKERFILE at a pinned ref, which cannot build a commit that is not pushed yet. A flag
# replaces that clone stage with a local tree via buildx's --build-context, so both paths reach
# the same COPY. A build that used one says so in its log, because the resulting image no longer
# corresponds to the pin.
#
# roqsim is not a public repository yet, so the clone needs a token: set GITHUB_TOKEN (or
# ROBOVAST_GIT_TOKEN) and it is passed as a BuildKit secret, or use --roqsim-src.

BASEDIR=$(cd "$(dirname "$0")" && pwd)
ROS_DISTRO="jazzy"
PROJECT=""
IMAGE="robovast"
# The tag every image in this run is published under. `latest` because that is what CI
# publishes on every merge to the default branch, and therefore what the built-in family
# default resolves to -- a build.sh that produced some other tag by default would leave
# ROBOVAST_PROJECT pointing at an image nobody built.
#
# Pass --tag <stamp> to publish an immutable set instead: that is how a deployment is
# pinned (ROBOVAST_PROJECT_TAG=<stamp>), since one tag covers the whole family and so
# cannot be four different digests. It is also how a second ROS distro gets its own
# images, the job the old `_${ROS_DISTRO}` name suffix did.
TAG="latest"
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
    --tag)
      TAG="$2"
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
# `library/robovast` to Docker Hub and fail with "push access denied". The old
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
    #
    # Pinned to the `docker` driver rather than inheriting whichever builder the operator has
    # selected, for the reason robovast.service.image_store._LOCAL_BUILDER gives at length: only
    # that driver runs inside dockerd, so only it can see images the daemon already has. On a
    # docker-container builder (a multi-arch one is a common thing to have selected) the 5.6 GB
    # ROS base is invisible and gets re-fetched from the registry on every build -- and on a
    # builder whose container cannot resolve DNS it does not merely re-fetch, it fails outright
    # while the image sits in the local daemon. EXTRA_ARGS is appended after these, so an explicit
    # `-- --builder X` still wins. Not applied to --push: a multi-platform push needs the
    # container driver, which is the one thing the docker driver cannot do.
    BUILDX_ARGS+=(--builder default --load -t "$local_tag")
    # An `[[ ... ]] && ...` as the LAST statement becomes the function's exit status. The two tags
    # are equal exactly when no --project was given -- the plain local build this script documents
    # first -- so the test failed, the function returned 1, and every caller's `|| return $?`
    # aborted before docker was invoked at all. It looked like a build that printed its header and
    # silently did nothing.
    if [[ -n "$published_tag" && "$published_tag" != "$local_tag" ]]; then
      BUILDX_ARGS+=(-t "$published_tag")
    fi
  fi
}

echo "Using Dockerfile: $BASEDIR"
echo "From Context: $PWD"
echo "Project: $PROJECT"
echo "Tag: $TAG"
echo "Image(s): $IMAGE"

# ensure PROJECT ends with a slash when non-empty
if [[ -n "${PROJECT}" ]]; then
  [[ "${PROJECT}" == */ ]] || PROJECT="${PROJECT}/"
fi

# One throwaway directory for both jobs: the empty build context every image is built against
# (nothing is read from the context -- see build_base), and a home for any working tree
# src_context stages for --build-context. Removed on every exit path, including a failed build.
SRC_STAGING=$(mktemp -d) || exit 1
trap 'rm -rf "$SRC_STAGING"' EXIT
EMPTY_CTX="$SRC_STAGING/empty"
mkdir -p "$EMPTY_CTX"

# A --secret for the Dockerfiles' clone stages, when a token is available. Needed while roqsim
# is not a public repository: without it the clone gets git prompting for a password on a
# terminal that does not exist. Nothing here fails when no token is set -- a public repo needs
# none, and a --roqsim-src build never clones.
#
# env= rather than the value on the command line, so it does not reach the process table.
git_secret() {
  GIT_SECRET=()
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    GIT_SECRET=(--secret id=git_token,env=GITHUB_TOKEN)
  elif [[ -n "${ROBOVAST_GIT_TOKEN:-}" ]]; then
    GITHUB_TOKEN="$ROBOVAST_GIT_TOKEN" && export GITHUB_TOKEN
    GIT_SECRET=(--secret id=git_token,env=GITHUB_TOKEN)
  fi
}

# A source override for one of the Dockerfile's ``*-src`` stages, or nothing.
#
# ``--build-context <stage>=<dir>`` replaces that stage with a local tree, which is buildx's
# own mechanism for exactly this and leaves the Dockerfile with ONE code path: the default
# clone and a working checkout arrive at the same COPY. The alternative -- staging a directory
# into a temp context and branching on whether it is empty -- is what used to be here, and it
# made the Dockerfiles unbuildable by anything but this script, CI included.
#
# rsync rather than passing the path straight through, because the excludes matter: a .git of
# several hundred MB, a host venv whose binaries are wrong for the image, colcon build/ and
# install/ trees that would be found ahead of what colcon builds inside the image, and (for
# roqsim) `external/`, ~250 MB of vendored upstream source that nothing installed below reads.
src_context() {
  local stage="$1" src="$2" label="$3"
  SRC_CONTEXT=()
  if [[ -z "$src" ]]; then
    echo "$label source: clone at the Dockerfile's pinned ref"
    return 0
  fi
  echo "$label source: $src (local checkout)"
  local staged="$SRC_STAGING/$stage"
  mkdir -p "$staged" || return 1
  rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
        --exclude='*.egg-info' --exclude='build/' --exclude='install/' --exclude='log/' \
        --exclude='external/' --exclude='docs/build/' \
        "${src%/}/" "$staged/" || return 1
  SRC_CONTEXT=(--build-context "$stage=$staged")
}

build_base() {
  # An empty context: every input this Dockerfile reads arrives as a build stage or a
  # --build-context, so there is nothing for the daemon to receive. Passing the repo root sent
  # the whole working tree (frontend/ui/node_modules included) on every build for no reason.
  src_context scenario-execution-src "$SCENARIO_EXECUTION_SRC" scenario-execution || return $?
  git_secret

  buildx_args "${PLATFORM:-${PUSH:+$CLUSTER_PLATFORM}}" \
    "robovast:${TAG}" "${PROJECT}robovast:${TAG}" || return $?

  docker buildx build \
    "${BUILDX_ARGS[@]}" \
    "${SRC_CONTEXT[@]}" \
    "${GIT_SECRET[@]}" \
    --build-arg ROS_DISTRO=$ROS_DISTRO \
    $EXTRA_ARGS \
    -f $BASEDIR/Dockerfile \
    "$EMPTY_CTX"
}

build_roqsim() {
  # FROM the base by its *resolved* tag rather than a floating one, so the derived image
  # is always built on the base that was just built (or the one explicitly named) and
  # the two cannot drift. It inherits /etc/robovast_compat_version from there, which is
  # why the compat check needs no second home.
  local base="${BASE_IMAGE:-${PROJECT}robovast:${TAG}}"

  src_context roqsim-src "$ROQSIM_SRC" roqsim || return $?
  git_secret
  # ROQSIM_REF only reaches the clone, so it is meaningless alongside a local checkout --
  # saying so beats building the wrong tree and reporting success.
  if [[ -n "$ROQSIM_SRC" && -n "${ROQSIM_REF:-}" ]]; then
    echo "note: ROQSIM_REF=$ROQSIM_REF ignored -- --roqsim-src takes precedence" >&2
  elif [[ -z "$ROQSIM_SRC" ]]; then
    # Resolve the ref to a commit BEFORE building, and pass that.
    #
    # Not a nicety: the clone happens in a build layer whose cache key is the command text and
    # its ARGs, so with a branch name in ROQSIM_REF the cache serves the tree from the first
    # build forever -- a week later the same command silently produces the same stale roqsim,
    # and the image looks freshly built. Passing the sha makes the key change exactly when the
    # remote does.
    #
    # It also turns the build log into a record of what was built, which a branch name is not.
    local ref="${ROQSIM_REF:-main}" resolved
    if [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then
      resolved="$ref"
    else
      resolved=$(git ls-remote "$ROQSIM_REPO" "$ref" 2>/dev/null | awk 'NR==1{print $1}')
      if [[ -z "$resolved" ]]; then
        # Do not fall back to the branch name: that would quietly restore the stale-cache
        # behaviour this exists to remove.
        echo "error: cannot resolve '$ref' in $ROQSIM_REPO." >&2
        echo "       roqsim is public, so this is a ref that does not exist rather than an" >&2
        echo "       access problem -- check the branch/tag name, or use --roqsim-src <path>" >&2
        echo "       to build from a checkout." >&2
        return 1
      fi
      echo "roqsim ref: $ref -> $resolved"
    fi
    ROQSIM_REF="$resolved"
  fi

  buildx_args "${PLATFORM:-${PUSH:+$CLUSTER_PLATFORM}}" \
    "robovast-roqsim:${TAG}" "${PROJECT}robovast-roqsim:${TAG}" || return $?

  docker buildx build \
    "${BUILDX_ARGS[@]}" \
    "${SRC_CONTEXT[@]}" \
    "${GIT_SECRET[@]}" \
    --build-arg BASE_IMAGE="$base" \
    ${ROQSIM_REF:+--build-arg ROQSIM_REF="$ROQSIM_REF"} \
    ${ROQSIM_REPO:+--build-arg ROQSIM_REPO="$ROQSIM_REPO"} \
    $EXTRA_ARGS \
    -f $BASEDIR/Dockerfile.roqsim \
    "$EMPTY_CTX"
}

case "$IMAGE" in
  robovast) build_base ;;
  roqsim) build_roqsim ;;
  all)      build_base && build_roqsim ;;
esac
