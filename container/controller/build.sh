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
# Point a deployment at the resulting image by naming its project:
#   export ROBOVAST_PROJECT=docker.io/<you>   # moves the whole image family
# then 'vast service upgrade'. Build the other three to match with
# 'make release-images PROJECT=docker.io/<you> PUSH=1'.
BASEDIR=$(dirname "$0")
ROOT=$(cd "$BASEDIR/../.." && pwd)

TAG="robovast-controller:latest"
PUSH=""
PLATFORM=""

# shellcheck source=../platforms.env
. "$ROOT/container/platforms.env"
# shellcheck source=../buildcache.sh
. "$ROOT/container/buildcache.sh"
# shellcheck source=container/ask_push.sh
. "$ROOT/container/ask_push.sh"
# shellcheck source=../git_revision.sh
. "$ROOT/container/git_revision.sh"

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
    --ask-push)
      ASK_PUSH=1
      shift
      ;;
    *)
      break
      ;;
  esac
done

ask_push "$TAG"

# A push publishes for the cluster, which is linux/amd64 -- so honour the declared
# policy rather than the host's architecture. Building on an arm64 Mac otherwise
# pushes an image no node can run, and the failure surfaces as an exec-format error
# in a pod rather than here.
[[ -n "$PUSH" && -z "$PLATFORM" ]] && PLATFORM="$CLUSTER_PLATFORM"

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

# Registry layer cache for this image's own repo. It protects the two node stages and the
# poetry pass over the scientific stack, which is the bulk of this build and is rebuilt from
# scratch whenever the local builder has lost its cache.
buildcache_args "$TAG" "${PUSH:-}"

# The service image is the one asked "is the change I just made loaded?", so it is the one that
# must carry the answer: the `ENV` this sets is all `code_revision()` has to go on in a pod,
# where there is no `.git` above site-packages to ask instead.
git_revision_args "$ROOT"

docker buildx build \
  "${BUILDX_ARGS[@]}" \
  "${BUILDCACHE_ARGS[@]}" \
  "${GIT_REVISION_ARGS[@]}" \
  $EXTRA_ARGS \
  -t "$TAG" \
  -f "$BASEDIR/Dockerfile" \
  "$ROOT"
