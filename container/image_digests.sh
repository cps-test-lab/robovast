#!/bin/bash -e
# Report the images in a registry as repo@sha256:... env lines, ready to paste into .env.
#
# Why this exists next to release_images.sh, which already prints digests: that script
# reports what *it just built*, and prefers the local image's RepoDigests precisely
# because a fresh push is the only moment a floating :latest is known to be the thing in
# hand. An operator bringing up a second cluster has built nothing and has no local
# images at all, so the only available truth is the registry.
#
# The difference is deliberate, not duplication: here the floating tag *is* the question
# ("what does :latest point at right now, so I can stop depending on :latest?"), whereas
# during a release run resolving the tag would name whatever older image it still pointed
# at. Same output shape, opposite source, for opposite reasons.
#
# Usage:
#   container/image_digests.sh --project docker.io/freeedlabs [--ros-distro jazzy]

PROJECT=""
ROS_DISTRO="${ROS_DISTRO:-jazzy}"

while [[ $# -gt 0 ]]; do
  case $1 in
    --project)    PROJECT="$2"; shift 2 ;;
    --ros-distro) ROS_DISTRO="$2"; shift 2 ;;
    -h|--help)    sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$PROJECT" ]]; then
  echo "ERROR: --project is required (e.g. --project docker.io/freeedlabs)" >&2
  exit 2
fi
# Match release_images.sh's normalization, so the same --project value works for both.
[[ "$PROJECT" != */ ]] && PROJECT="${PROJECT}/"

# The env var each image is read from, paired with its repository name. These variable
# names are the contract with robovast.common.execution._resolve_image; a test keeps this
# list and that reader from drifting apart.
# The sidecar is not ROS-distro suffixed: it is alpine + mc + boto3, with nothing in it
# that a distro could change.
declare -a VARS=(ROBOVAST_IMAGE ROBOVAST_CONTROLLER_IMAGE ROBOVAST_ROQSIM_IMAGE ROBOVAST_SIDECAR_IMAGE)
declare -a REPOS=("robovast_${ROS_DISTRO}" "robovast-controller" "robovast_roqsim_${ROS_DISTRO}" "robovast-sidecar")

missing=()
lines=()

for i in "${!VARS[@]}"; do
  repo="${PROJECT}${REPOS[$i]}"
  # imagetools reads the registry, so this works on a machine that has never built or
  # pulled anything -- which is the whole point of the target.
  digest=$(docker buildx imagetools inspect --format '{{.Manifest.Digest}}' "${repo}:latest" 2>/dev/null || true)
  if [[ "$digest" == sha256:* ]]; then
    lines+=("${VARS[$i]}=${repo}@${digest}")
  else
    missing+=("${repo}:latest")
  fi
done

# A partial answer is worse than none here: the lines print in .env syntax, so a reader
# copying three lines when four were expected pins two images and silently leaves the
# third floating -- the exact failure pinning is meant to prevent.
if [[ ${#missing[@]} -gt 0 ]]; then
  echo >&2
  echo "ERROR: no digest in the registry for:" >&2
  printf '  %s\n' "${missing[@]}" >&2
  echo >&2
  echo "Either the tag was never pushed to ${PROJECT%/}, or this machine cannot read it" >&2
  echo "(private registry: run 'docker login'). Publish with:" >&2
  echo "  make release-images PROJECT=${PROJECT%/} PUSH=1" >&2
  exit 1
fi

echo "# Images in ${PROJECT%/}, pinned by digest so the version a campaign ran on stays a"
echo "# recorded fact. Re-run 'make image-digests PROJECT=${PROJECT%/}' after a new release."
printf '%s\n' "${lines[@]}"
