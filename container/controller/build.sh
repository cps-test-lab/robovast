#!/bin/bash -e
# Convenience build for the robovast-controller image.
#
# The controller image installs robovast + its Python dependencies, so the build
# context must be the repository root (pyproject.toml + src/ are needed for
# `pip install .`). The image is self-contained (FROM python:3.12-slim), so only
# the controller image needs to be pushed (e.g. to Docker Hub).
#
# Examples:
#   container/controller/build.sh
#   container/controller/build.sh -t docker.io/<you>/robovast-controller:dev --push
#
# Point a run at the resulting image via:  export ROBOVAST_CONTROLLER_IMAGE=<tag>
BASEDIR=$(dirname "$0")
ROOT=$(cd "$BASEDIR/../.." && pwd)

TAG="robovast-controller:latest"
PUSH=""
PLATFORM=""

# shellcheck source=../platforms.env
. "$ROOT/container/platforms.env"

while [[ $# -gt 0 ]]; do
  case $1 in
    -t|--tag)
      TAG="$2"
      shift 2
      ;;
    --platform)
      PLATFORM="$2"
      shift 2
      ;;
    --push|-n)
      PUSH=1
      shift
      ;;
    *)
      break
      ;;
  esac
done

# A push publishes for the cluster, which is linux/amd64 -- so honour the declared
# policy rather than the host's architecture. Building on an arm64 Mac otherwise
# pushes an image no node can run, and the failure surfaces as an exec-format error
# in a pod rather than here.
[[ -n "$PUSH" && -z "$PLATFORM" ]] && PLATFORM="$PLATFORMS_CONTROLLER"

# Pass remaining arguments to docker build
EXTRA_ARGS="$@"

echo "Using Dockerfile: $BASEDIR/Dockerfile"
echo "From context:     $ROOT"
echo "Target tag:       $TAG"
echo "Platform:         ${PLATFORM:-<host>}"

# buildx, the same builder CI uses, so a local image and a published one are produced
# the same way. A multi-platform build cannot --load into the local daemon (there is no
# single image to load), so it is only valid with --push; a host-architecture build
# loads, which is what makes a locally built image runnable straight away.
BUILDX_ARGS=()
[[ -n "$PLATFORM" ]] && BUILDX_ARGS+=(--platform "$PLATFORM")
if [[ -n "${PUSH:-}" ]]; then
  BUILDX_ARGS+=(--push)
elif [[ "$PLATFORM" == *,* ]]; then
  echo "refusing to build $PLATFORM without --push: a multi-platform image cannot be loaded into the local docker daemon" >&2
  exit 2
else
  BUILDX_ARGS+=(--load)
fi

docker buildx build \
  "${BUILDX_ARGS[@]}" \
  $EXTRA_ARGS \
  -t "$TAG" \
  -f "$BASEDIR/Dockerfile" \
  "$ROOT"
