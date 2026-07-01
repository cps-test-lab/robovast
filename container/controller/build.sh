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

while [[ $# -gt 0 ]]; do
  case $1 in
    -t|--tag)
      TAG="$2"
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

# Pass remaining arguments to docker build
EXTRA_ARGS="$@"

echo "Using Dockerfile: $BASEDIR/Dockerfile"
echo "From context:     $ROOT"
echo "Target tag:       $TAG"

DOCKER_BUILDKIT=1 docker build \
  $EXTRA_ARGS \
  -t "$TAG" \
  -f "$BASEDIR/Dockerfile" \
  "$ROOT"

if [ -n "${PUSH:-}" ]; then
  echo "Pushing docker image to $TAG"
  docker push "$TAG"
fi
