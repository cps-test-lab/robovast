#!/bin/bash -e
# Report what a project's RoboVAST image family currently resolves to, from the registry.
#
# Answers one question: "is this project/tag a complete, pullable set, and what is in it
# right now?" -- before a cluster finds out by failing to pull. Reading the registry rather
# than local images is the whole point: an operator bringing up a second cluster has built
# nothing and has no local images at all.
#
# It reports; it does not configure. Configuration is two lines (ROBOVAST_PROJECT and
# ROBOVAST_PROJECT_TAG), and a *tag* is what pins a deployment -- one tag covers all four
# members, so it cannot be four different digests. The digests below are for the record: a
# release note, or checking that two clusters really run the same images.
#
# release_images.sh prints digests too, but for what it just built, preferring the local
# image's RepoDigests because a fresh push is the only moment a floating tag is known to be
# the thing in hand. Here the tag itself is the question. Opposite sources, opposite reasons.
#
# Usage:
#   container/image_digests.sh --project docker.io/freeedlabs [--tag latest]

PROJECT=""
TAG="latest"

while [[ $# -gt 0 ]]; do
  case $1 in
    --project) PROJECT="$2"; shift 2 ;;
    --tag)     TAG="$2"; shift 2 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$PROJECT" ]]; then
  echo "ERROR: --project is required (e.g. --project docker.io/freeedlabs)" >&2
  exit 2
fi
# Match release_images.sh's normalization, so the same --project value works for both.
[[ "$PROJECT" != */ ]] && PROJECT="${PROJECT}/"

# The family members, exactly as robovast.common.execution.FAMILY_MEMBERS spells them; a
# test keeps this list and that one from drifting apart. Not ROS-distro suffixed: the distro
# is a tag, so a second distro is a second --tag rather than four more repositories.
declare -a MEMBERS=(robovast robovast-roqsim robovast-controller robovast-sidecar)

missing=()
lines=()

for member in "${MEMBERS[@]}"; do
  repo="${PROJECT}${member}"
  # imagetools reads the registry, so this works on a machine that has never built or
  # pulled anything -- which is the whole point of the target.
  digest=$(docker buildx imagetools inspect --format '{{.Manifest.Digest}}' "${repo}:${TAG}" 2>/dev/null || true)
  if [[ "$digest" == sha256:* ]]; then
    lines+=("${repo}:${TAG} -> ${digest}")
  else
    missing+=("${repo}:${TAG}")
  fi
done

# An incomplete set is an error, not a footnote: ROBOVAST_PROJECT moves all four members at
# once, so a project missing one of them is not usable at all -- and the way that surfaces
# otherwise is a pod in ImagePullBackOff partway through a campaign.
if [[ ${#missing[@]} -gt 0 ]]; then
  if [[ ${#lines[@]} -gt 0 ]]; then
    echo "Present in ${PROJECT%/}:"
    printf '  %s\n' "${lines[@]}"
    echo
  fi
  echo "ERROR: ${#missing[@]} of ${#MEMBERS[@]} members are not in the registry at :${TAG}" >&2
  printf '  %s\n' "${missing[@]}" >&2
  echo >&2
  echo "ROBOVAST_PROJECT=${PROJECT%/} moves the whole family, so this project cannot serve a" >&2
  echo "campaign until every member is there. Either the tag was never pushed, or this machine" >&2
  echo "cannot read it (private registry: run 'docker login'). Publish with:" >&2
  echo "  make release-images PROJECT=${PROJECT%/} TAG=${TAG} PUSH=1" >&2
  exit 1
fi

echo "# The image family in ${PROJECT%/} at :${TAG} -- a complete, pullable set."
printf '#   %s\n' "${lines[@]}"
echo "#"
if [[ "$TAG" == "latest" ]]; then
  echo "# :latest floats, so these digests are what it points at *now*. To stop depending on"
  echo "# that, publish an immutable tag and pin it:"
  echo "#   make release-images PROJECT=${PROJECT%/} TAG=\$(date +%F) PUSH=1"
fi
echo "ROBOVAST_PROJECT=${PROJECT%/}"
echo "ROBOVAST_PROJECT_TAG=${TAG}"
