#!/bin/bash -e
# Build and push all four of robovast's own container images (base, roqsim, controller,
# sidecar) to one registry/project prefix, in one call. The local-dev counterpart of what
# .github/workflows/image.yml does per-image in CI, collapsed into a single command
# for deploying to an arbitrary registry (Docker Hub, a fork's GHCR, ...).
#
# Orchestrates the existing per-image scripts rather than reimplementing any
# docker build/push logic:
#   container/robovast/build.sh    (base + roqsim, via --image all)
#   container/controller/build.sh  (controller)
#
# The sidecar has no build.sh, so it is built with buildx directly. It used to be left
# out for that reason -- which meant a release to a dev registry published three images
# and silently kept the public sidecar, the same "three of the four" gap that
# ROBOVAST_SIDECAR_IMAGE was added to close.
#
# Usage:
#   ./container/release_images.sh --project <prefix> [--push] [--ros-distro <distro>] \
#                                  [--roqsim-ref <ref> | --roqsim-src <path>] \
#                                  [-- <extra docker build args>]
#
# Example:
#   ./container/release_images.sh --project docker.io/freeedlabs --push
#
# --roqsim-src builds the simulator image from a checkout on disk instead of cloning, for
# a caller that already has one -- a superproject holding roqsim as a submodule, or an
# unpushed commit. It is the same option container/robovast/build.sh takes; this script
# only forwards it. Mutually exclusive with --roqsim-ref, which names a commit to clone.

BASEDIR=$(cd "$(dirname "$0")" && pwd)

PROJECT=""
PUSH=""
ROS_DISTRO="jazzy"
ROQSIM_REF="robovast"
ROQSIM_SRC=""

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
    --roqsim-ref)
      ROQSIM_REF="$2"
      ROQSIM_REF_SET=1
      shift 2
      ;;
    --roqsim-src)
      ROQSIM_SRC="$2"
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
  echo "Usage: $0 --project <registry/namespace> [--push] [--ros-distro <distro>] [--roqsim-ref <ref> | --roqsim-src <path>] [-- <extra docker build args>]" >&2
  echo "Example: $0 --project docker.io/freeedlabs --push" >&2
}

if [[ -z "$PROJECT" ]]; then
  usage
  exit 2
fi

if [[ -n "$ROQSIM_SRC" && -n "$ROQSIM_REF_SET" ]]; then
  echo "error: --roqsim-src and --roqsim-ref both given; one checkout, one clone -- pick one." >&2
  exit 2
fi

if [[ -n "$ROQSIM_SRC" ]]; then
  # Resolved here because build.sh runs from its own directory and the caller's relative
  # path (a plain ./roqsim from a superproject) would otherwise resolve somewhere else.
  ROQSIM_SRC=$(cd "$ROQSIM_SRC" 2>/dev/null && pwd) || {
    echo "error: --roqsim-src path does not exist" >&2; exit 2; }
  # A wrong-but-present directory is the failure worth catching: it would build an image
  # missing the packages rather than fail, and the image is what campaigns pin.
  [[ -f "$ROQSIM_SRC/roqsim/pyproject.toml" ]] || {
    echo "error: $ROQSIM_SRC is not a roqsim checkout (no roqsim/pyproject.toml)" >&2; exit 2; }
fi

# ensure PROJECT ends with a slash -- matches container/robovast/build.sh's own
# normalization, needed here too since container/controller/build.sh takes a full
# tag rather than a prefix and this script builds that tag itself.
[[ "$PROJECT" == */ ]] || PROJECT="${PROJECT}/"

PUSH_FLAG=()
[[ -n "$PUSH" ]] && PUSH_FLAG=(--push)

BASE_TAG="${PROJECT}robovast_${ROS_DISTRO}:latest"
ROQSIM_TAG="${PROJECT}robovast_roqsim_${ROS_DISTRO}:latest"
CONTROLLER_TAG="${PROJECT}robovast-controller:latest"
# Not ROS-distro suffixed: alpine + mc + boto3, with nothing a distro could change.
SIDECAR_TAG="${PROJECT}robovast-sidecar:latest"

SRC_FLAG=()
[[ -n "$ROQSIM_SRC" ]] && SRC_FLAG=(--roqsim-src "$ROQSIM_SRC")

echo "== base + roqsim =="
ROQSIM_REF="$ROQSIM_REF" "$BASEDIR/robovast/build.sh" --image all --project "$PROJECT" \
  --ros-distro "$ROS_DISTRO" "${SRC_FLAG[@]}" "${PUSH_FLAG[@]}" -- $EXTRA_ARGS

echo
echo "== controller =="
# No "--" separator here: unlike robovast/build.sh, controller/build.sh has no case for
# it -- an unrecognized "--" would fall through to its own EXTRA_ARGS and get injected
# into its docker build call raw, corrupting the argument list.
"$BASEDIR/controller/build.sh" -t "$CONTROLLER_TAG" "${PUSH_FLAG[@]}" $EXTRA_ARGS

echo
echo "== sidecar =="
# Built here rather than only in CI so a dev-registry release is complete. It was the one
# image release_images.sh did not publish, which meant an operator pointing PROJECT at
# their own registry got three dev images and silently kept the published sidecar --
# and, until the service started carrying ROBOVAST_SIDECAR_IMAGE into its pod, had no way
# to notice. Buildx directly: there is no container/sidecar/build.sh.
# shellcheck source=container/platforms.env
. "$BASEDIR/platforms.env"
docker buildx build --platform "$PLATFORMS_SIDECAR" \
  -t "$SIDECAR_TAG" "${PUSH_FLAG[@]}" "$BASEDIR/sidecar" $EXTRA_ARGS

# The digest for a repo:tag, as repo@sha256:... -- printed instead of the floating tag
# below, matching this repo's own pin-by-digest convention (see the roqsim image comment
# in configs/examples/basic_nav/basic_nav_roqsim.vast).
#
# Two sources, cheapest first:
#   1. the local image's RepoDigests. Set when this image was pushed to (or pulled from)
#      that repo, so it needs no registry round trip -- and its presence is itself the
#      proof that the local image really is in the registry under that digest. Consulted
#      whether or not --push was given: a fully cached rebuild yields the same image id
#      and therefore the same, still-valid, digest.
#   2. the registry itself, via buildx imagetools. Needed because a push does not always
#      leave a RepoDigest behind (containerd image store, a buildx --push builder). Only
#      trusted right after our own push, when the tag in the registry is by definition
#      what we just wrote -- resolving it without a push would name whatever older image
#      the floating tag still points at, which is exactly the confusion digests prevent.
#
# Matches against both the full repo and the repo with a leading "docker.io/" stripped:
# Docker normalizes docker.io (the implicit default registry) out of RepoDigests entries,
# so a --project docker.io/... repo would otherwise never match its own digest.
image_ref() {
  # ${1%:*} rather than ${1%:latest}: the tag is :latest today, but a repo whose tag was
  # not stripped matches no RepoDigests entry at all -- it would report "no digest" for an
  # image that has one. A registry port (host:5000/ns/img:tag) survives this, since only
  # the last colon-suffix is removed.
  local tag="$1" repo="${1%:*}" repo_norm="${1%:*}" digest
  repo_norm="${repo_norm#docker.io/}"
  digest=$(docker inspect --format='{{range .RepoDigests}}{{println .}}{{end}}' "$tag" 2>/dev/null \
    | grep -F -e "${repo}@" -e "${repo_norm}@" | tail -1)

  if [[ -z "$digest" && -n "$PUSH" ]]; then
    local manifest_digest
    manifest_digest=$(docker buildx imagetools inspect --format '{{.Manifest.Digest}}' "$tag" 2>/dev/null)
    [[ "$manifest_digest" == sha256:* ]] && digest="${repo}@${manifest_digest}"
  fi

  echo "$digest"
}

resolve_refs() {
  local name tag ref
  MISSING_DIGESTS=()
  for name in BASE ROQSIM_IMAGE CONTROLLER_IMAGE SIDECAR_IMAGE; do
    case "$name" in
      BASE)             tag="$BASE_TAG" ;;
      ROQSIM_IMAGE)   tag="$ROQSIM_TAG" ;;
      CONTROLLER_IMAGE) tag="$CONTROLLER_TAG" ;;
      SIDECAR_IMAGE)    tag="$SIDECAR_TAG" ;;
    esac
    ref=$(image_ref "$tag")
    if [[ -z "$ref" ]]; then
      MISSING_DIGESTS+=("$tag")
      ref="$tag"
    fi
    printf -v "${name}_REF" '%s' "$ref"
  done
}

resolve_refs

# A --push run whose digest cannot be resolved is a failure, not a footnote: the tags
# printed below would be indistinguishable from a successful digest run for anyone
# copying them into a .vast or .env, and a floating :latest silently changes what a
# campaign ran on. Say so and exit non-zero rather than hand out an unpinnable ref.
if [[ -n "$PUSH" && ${#MISSING_DIGESTS[@]} -gt 0 ]]; then
  echo >&2
  echo "ERROR: pushed, but could not resolve a digest for:" >&2
  printf '  %s\n' "${MISSING_DIGESTS[@]}" >&2
  echo "Neither the local image's RepoDigests nor 'docker buildx imagetools inspect' named one." >&2
  echo "Check that the push actually reached ${PROJECT%/} (credentials, 'docker login') and retry." >&2
  exit 1
fi

echo
echo "== done =="
if [[ -n "$PUSH" ]]; then
  echo "Built and pushed:"
else
  echo "Built (local only -- re-run with --push to publish to ${PROJECT%/}):"
fi
echo "  $BASE_TAG"
echo "  $ROQSIM_TAG"
echo "  $CONTROLLER_TAG"
echo
if [[ ${#MISSING_DIGESTS[@]} -eq 0 ]]; then
  echo "Referenced below by digest rather than the floating :latest tag, so the image a run"
  echo "actually uses stays a recorded fact:"
else
  # Only reachable without --push (a --push run with a missing digest exited above). A
  # mixed list is normal there: an image whose rebuild was fully cached still carries the
  # RepoDigest of its earlier push and is pinnable; a rebuilt one is not in the registry.
  echo "${#MISSING_DIGESTS[@]} of 4 images are not in ${PROJECT%/} under a digest and are named by"
  echo ":latest below -- there is nothing pinnable for them yet. Re-run with --push to publish"
  echo "them and get repo@sha256:... refs instead:"
fi
echo "  ROBOVAST_IMAGE=${BASE_REF}"
echo "  ROBOVAST_ROQSIM_IMAGE=${ROQSIM_IMAGE_REF}"
echo "  ROBOVAST_CONTROLLER_IMAGE=${CONTROLLER_IMAGE_REF}"
echo "  ROBOVAST_SIDECAR_IMAGE=${SIDECAR_IMAGE_REF}"
echo "Note: a .vast file's own 'image:' field overrides .env/env vars -- edit it directly if a"
echo "campaign pins its image explicitly (as configs/examples/basic_nav/*.vast do)."

# Offer to write the four lines above into ./.env -- the file `vast` itself loads (see
# src/robovast/common/env_file.py: current directory only, current directory when `vast`
# runs). Only ever touches these four keys in place; any other line (e.g. registry
# credentials) is left untouched. Skipped outside an interactive terminal (e.g. CI) rather
# than hanging on a read that will never come.
set_env_var() {
  local key="$1" value="$2" file="${3:-.env}"
  touch "$file"
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    echo "${key}=${value}" >> "$file"
  fi
}

if [[ -t 0 ]]; then
  echo
  read -r -p "Update ./.env with these 4 lines now? [y/N] " REPLY || REPLY=""
  if [[ "$REPLY" =~ ^[Yy] ]]; then
    set_env_var ROBOVAST_IMAGE "$BASE_REF"
    set_env_var ROBOVAST_ROQSIM_IMAGE "$ROQSIM_IMAGE_REF"
    set_env_var ROBOVAST_CONTROLLER_IMAGE "$CONTROLLER_IMAGE_REF"
    set_env_var ROBOVAST_SIDECAR_IMAGE "$SIDECAR_IMAGE_REF"
    echo "Updated ./.env."
  fi
else
  echo
  echo "(non-interactive -- run this script directly in a terminal to be offered an automatic .env update)"
fi
