#!/bin/bash -e
# Build and push all of robovast's own container images (base, robosito, controller)
# to one registry/project prefix, in one call. The local-dev counterpart of what
# .github/workflows/image.yml does per-image in CI, collapsed into a single command
# for deploying to an arbitrary registry (Docker Hub, a fork's GHCR, ...).
#
# Orchestrates the two existing per-image scripts rather than reimplementing any
# docker build/push logic:
#   container/robovast/build.sh    (base + robosito, via --image all)
#   container/controller/build.sh  (controller)
#
# sidecar is intentionally excluded: it has no build.sh of its own today (CI-only).
#
# Usage:
#   ./container/release_images.sh --project <prefix> [--push] [--ros-distro <distro>] \
#                                  [--robosito-ref <ref>] [-- <extra docker build args>]
#
# Example:
#   ./container/release_images.sh --project docker.io/freeedlabs --push

BASEDIR=$(cd "$(dirname "$0")" && pwd)

PROJECT=""
PUSH=""
ROS_DISTRO="jazzy"
ROBOSITO_REF="robovast"

while [[ $# -gt 0 ]]; do
  case $1 in
    --project)
      PROJECT="$2"
      shift 2
      ;;
    --push|-n)
      PUSH=1
      shift
      ;;
    --ros-distro)
      ROS_DISTRO="$2"
      shift 2
      ;;
    --robosito-ref)
      ROBOSITO_REF="$2"
      shift 2
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

EXTRA_ARGS="$@"

usage() {
  echo "Usage: $0 --project <registry/namespace> [--push] [--ros-distro <distro>] [--robosito-ref <ref>] [-- <extra docker build args>]" >&2
  echo "Example: $0 --project docker.io/freeedlabs --push" >&2
}

if [[ -z "$PROJECT" ]]; then
  usage
  exit 2
fi

# ensure PROJECT ends with a slash -- matches container/robovast/build.sh's own
# normalization, needed here too since container/controller/build.sh takes a full
# tag rather than a prefix and this script builds that tag itself.
[[ "$PROJECT" == */ ]] || PROJECT="${PROJECT}/"

PUSH_FLAG=()
[[ -n "$PUSH" ]] && PUSH_FLAG=(--push)

BASE_TAG="${PROJECT}robovast_${ROS_DISTRO}:latest"
ROBOSITO_TAG="${PROJECT}robovast_robosito_${ROS_DISTRO}:latest"
CONTROLLER_TAG="${PROJECT}robovast-controller:latest"

echo "== base + robosito =="
ROBOSITO_REF="$ROBOSITO_REF" "$BASEDIR/robovast/build.sh" --image all --project "$PROJECT" \
  --ros-distro "$ROS_DISTRO" "${PUSH_FLAG[@]}" -- $EXTRA_ARGS

echo
echo "== controller =="
# No "--" separator here: unlike robovast/build.sh, controller/build.sh has no case for
# it -- an unrecognized "--" would fall through to its own EXTRA_ARGS and get injected
# into its docker build call raw, corrupting the argument list.
"$BASEDIR/controller/build.sh" -t "$CONTROLLER_TAG" "${PUSH_FLAG[@]}" $EXTRA_ARGS

# The digest for a repo:tag we just pushed, as repo@sha256:... -- printed instead of the
# floating tag below, matching this repo's own pin-by-digest convention (see the robosito
# image comment in configs/examples/basic_nav/basic_nav_rst.vast): a push updates the
# local image's RepoDigests for that repo, so this needs no registry round trip. Falls
# back to the plain tag if no digest is found (e.g. --push was not given).
#
# Matches against both the full repo and the repo with a leading "docker.io/" stripped:
# Docker normalizes docker.io (the implicit default registry) out of RepoDigests entries,
# so a --project docker.io/... repo would otherwise never match its own digest.
image_ref() {
  local tag="$1" repo="${1%:latest}" repo_norm="${1%:latest}" digest
  repo_norm="${repo_norm#docker.io/}"
  digest=$(docker inspect --format='{{range .RepoDigests}}{{println .}}{{end}}' "$tag" 2>/dev/null \
    | grep -F -e "${repo}@" -e "${repo_norm}@" | tail -1)
  echo "${digest:-$tag}"
}

echo
echo "== done =="
if [[ -n "$PUSH" ]]; then
  echo "Built and pushed:"
else
  echo "Built (local only -- re-run with --push to publish to ${PROJECT%/}):"
fi
echo "  $BASE_TAG"
echo "  $ROBOSITO_TAG"
echo "  $CONTROLLER_TAG"
echo
if [[ -n "$PUSH" ]]; then
  echo "To point a run at these images, add to your project's .env (or export directly) --"
  echo "pinned by digest rather than the floating :latest tag, so the image a run actually"
  echo "uses stays a recorded fact:"
  echo "  ROBOVAST_IMAGE=$(image_ref "$BASE_TAG")"
  echo "  ROBOVAST_ROBOSITO_IMAGE=$(image_ref "$ROBOSITO_TAG")"
  echo "  ROBOVAST_CONTROLLER_IMAGE=$(image_ref "$CONTROLLER_TAG")"
else
  echo "To point a run at these images once pushed, add to your project's .env (or export"
  echo "directly) -- shown here by :latest since nothing was pushed; re-run with --push to"
  echo "get a pinnable digest instead:"
  echo "  ROBOVAST_IMAGE=${BASE_TAG}"
  echo "  ROBOVAST_ROBOSITO_IMAGE=${ROBOSITO_TAG}"
  echo "  ROBOVAST_CONTROLLER_IMAGE=${CONTROLLER_TAG}"
fi
echo "Note: a .vast file's own 'image:' field overrides .env/env vars -- edit it directly if a"
echo "campaign pins its image explicitly (as configs/examples/basic_nav/*.vast do)."
