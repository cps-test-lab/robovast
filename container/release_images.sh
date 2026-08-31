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
# out for that reason -- which meant a release to a dev registry published three images and
# silently kept the public sidecar. That gap is why the family is published as a *set*: one
# ROBOVAST_PROJECT moves all four, so there is no longer a per-image knob to forget.
#
# Usage:
#   ./container/release_images.sh --project <prefix> [--push|--ask-push] [--config-write] \
#                                  [--ros-distro <distro>] \
#                                  [--roqsim-ref <ref> | --roqsim-src <path>] \
#                                  [-- <extra docker build args>]
#
# Example:
#   ./container/release_images.sh --project ghcr.io/cps-test-lab --push
#
# --ask-push asks, on the terminal and before the first build, whether the family should be
# published -- what the Makefile passes when PUSH is not set, so publishing is a decision made
# out loud rather than a flag remembered. It has to be asked UP FRONT because `buildx --push`
# builds and publishes in one pass; there is no later moment where the images exist unpublished.
# Without a terminal to ask on it does not publish, which is the same thing as passing neither
# flag: a script that inherited this command must not push because nobody was there to say no.
#
# --roqsim-src builds the simulator image from a checkout on disk instead of cloning, for
# a caller that already has one -- a superproject holding roqsim as a submodule, or an
# unpushed commit. It is the same option container/robovast/build.sh takes; this script
# only forwards it. Mutually exclusive with --roqsim-ref, which names a commit to clone.

BASEDIR=$(cd "$(dirname "$0")" && pwd)

PROJECT=""
PUSH=""
ASK_PUSH=""
ROS_DISTRO="jazzy"
ROQSIM_REF="main"
ROQSIM_SRC=""
# `latest` matches CI and the built-in family default. Pass --tag <stamp> to publish an
# immutable set, which is how a deployment is pinned: ROBOVAST_PROJECT_TAG=<stamp>.
TAG="latest"
# Updating ~/.config/robovast/env is a convenience, not part of the release, and it changes
# which images every `vast` on this machine runs -- so it happens only when asked for:
# --config-write, or ROBOVAST_RELEASE_CONFIG_WRITE=1 for a shell that always wants it.
# Without it the two lines are printed and nothing on disk is touched.
CONFIG_WRITE="${ROBOVAST_RELEASE_CONFIG_WRITE:-}"

while [[ $# -gt 0 ]]; do
  case $1 in
    --project)
      PROJECT="$2"
      shift 2
      ;;
    --tag)
      TAG="$2"
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
    --config-write)
      CONFIG_WRITE=1
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
  echo "Usage: $0 --project <registry/namespace> [--tag <tag>] [--push|--ask-push] [--config-write] [--ros-distro <distro>] [--roqsim-ref <ref> | --roqsim-src <path>] [-- <extra docker build args>]" >&2
  echo "Example: $0 --project ghcr.io/cps-test-lab --push" >&2
  echo "Pinned:  $0 --project ghcr.io/cps-test-lab --tag 2026-08-17 --push" >&2
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

# shellcheck source=container/ask_push.sh
. "$BASEDIR/ask_push.sh"
# Asked with the destination in it, because "yes" here is the whole release decision: four images
# at one tag, moving whatever ROBOVAST_PROJECT resolves for everyone pointed at that tag. A bare
# "push? [y/N]" does not say which registry it means.
ask_push "all four family members to ${PROJECT}*:${TAG}"

PUSH_FLAG=()
[[ -n "$PUSH" ]] && PUSH_FLAG=(--push)

# The four family members, at one tag. One tag for the whole set is what makes
# ROBOVAST_PROJECT_TAG a single knob -- and why pinning a deployment means publishing an
# immutable tag rather than pasting four digests, which one tag cannot express.
BASE_TAG="${PROJECT}robovast:${TAG}"
ROQSIM_TAG="${PROJECT}robovast-roqsim:${TAG}"
CONTROLLER_TAG="${PROJECT}robovast-controller:${TAG}"
SIDECAR_TAG="${PROJECT}robovast-sidecar:${TAG}"

SRC_FLAG=()
[[ -n "$ROQSIM_SRC" ]] && SRC_FLAG=(--roqsim-src "$ROQSIM_SRC")

# shellcheck source=container/platforms.env
. "$BASEDIR/platforms.env"
# shellcheck source=container/buildcache.sh
. "$BASEDIR/buildcache.sh"

# Three jobs, not four steps. Only robovast-roqsim depends on anything: it is FROM the base,
# so those two are one chain. The controller (two npm builds plus poetry over the scientific
# stack) and the sidecar depend on nothing here, and running them after the ROS chain added
# their whole duration to a release for no reason.
#
# The chain keeps the terminal, because it is the long pole and a release with no output for
# ten minutes reads as hung. The other two are captured to logs and replayed in a fixed order
# when they finish, so the transcript is the same every run rather than three builds
# interleaving their lines unreadably.
#
# `wait <pid>` per job rather than a bare `wait`: a bare one returns the status of the LAST
# job only, so a controller failure behind a passing sidecar would vanish -- and with `-e`
# never firing on it, the script would sail on to report a published family that is missing a
# member. Each status is collected and checked explicitly below. `set -e` does not apply
# inside a background job's own subshell either, which is why nothing here relies on it.
LOG_DIR=$(mktemp -d)
trap 'rm -rf "$LOG_DIR"' EXIT

echo "== controller + sidecar (in the background; logs replayed below) =="

# No "--" separator here: unlike robovast/build.sh, controller/build.sh has no case for
# it -- an unrecognized "--" would fall through to its own EXTRA_ARGS and get injected
# into its docker build call raw, corrupting the argument list.
( "$BASEDIR/controller/build.sh" -t "$CONTROLLER_TAG" "${PUSH_FLAG[@]}" $EXTRA_ARGS ) \
  >"$LOG_DIR/controller.log" 2>&1 &
CONTROLLER_PID=$!

# Built here rather than only in CI so a dev-registry release is complete. It was the one
# image release_images.sh did not publish, which meant an operator pointing PROJECT at
# their own registry got three dev images and silently kept the published sidecar, with
# nothing to notice it by. A release must publish the whole family, because ROBOVAST_PROJECT
# moves the whole family. Buildx directly: there is no container/sidecar/build.sh.
buildcache_args "$SIDECAR_TAG" "$PUSH"
( docker buildx build --platform "$PLATFORMS_SIDECAR" \
    -t "$SIDECAR_TAG" "${PUSH_FLAG[@]}" "${BUILDCACHE_ARGS[@]}" \
    "$BASEDIR/sidecar" $EXTRA_ARGS ) \
  >"$LOG_DIR/sidecar.log" 2>&1 &
SIDECAR_PID=$!

echo
echo "== base + roqsim =="
# The one real dependency: --image all builds the base, then roqsim FROM the resolved base
# tag. Left in the foreground on purpose (see above). A failure here still has to wait for
# the background jobs before exiting, or `-e` would kill the script and orphan two builds
# mid-push; FAILED collects it instead.
CHAIN_STATUS=0
ROQSIM_REF="$ROQSIM_REF" "$BASEDIR/robovast/build.sh" --image all --project "$PROJECT" \
  --tag "$TAG" --ros-distro "$ROS_DISTRO" "${SRC_FLAG[@]}" "${PUSH_FLAG[@]}" -- $EXTRA_ARGS \
  || CHAIN_STATUS=$?

CONTROLLER_STATUS=0
wait "$CONTROLLER_PID" || CONTROLLER_STATUS=$?
SIDECAR_STATUS=0
wait "$SIDECAR_PID" || SIDECAR_STATUS=$?

echo
echo "== controller =="
cat "$LOG_DIR/controller.log"
echo
echo "== sidecar =="
cat "$LOG_DIR/sidecar.log"

# Every failure named, not just the first: three builds ran, so "it failed" without saying
# which one sends the reader to the wrong log. Reported before the digest resolution below,
# because a build that did not happen has no digest to resolve and its "no digest" line
# would otherwise be the only symptom.
FAILED=()
[[ $CHAIN_STATUS -eq 0 ]] || FAILED+=("base + roqsim (exit $CHAIN_STATUS)")
[[ $CONTROLLER_STATUS -eq 0 ]] || FAILED+=("controller (exit $CONTROLLER_STATUS)")
[[ $SIDECAR_STATUS -eq 0 ]] || FAILED+=("sidecar (exit $SIDECAR_STATUS)")
if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo >&2
  echo "ERROR: build failed:" >&2
  printf '  %s\n' "${FAILED[@]}" >&2
  exit 1
fi

# The digest for a repo:tag, as repo@sha256:... -- reported below, and used to verify that a
# --push actually landed. Not the configuration: one ROBOVAST_PROJECT_TAG covers all four
# members and so cannot be four digests; an immutable --tag is how a deployment is pinned.
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
  local tag ref
  MISSING_DIGESTS=()
  DIGEST_REFS=()
  for tag in "$BASE_TAG" "$ROQSIM_TAG" "$CONTROLLER_TAG" "$SIDECAR_TAG"; do
    ref=$(image_ref "$tag")
    if [[ -z "$ref" ]]; then
      MISSING_DIGESTS+=("$tag")
    else
      DIGEST_REFS+=("$ref")
    fi
  done
}

resolve_refs

# A --push run whose digest cannot be resolved is a failure, not a footnote: the digest is
# the only evidence the push reached the registry, so without it "== done ==" would report a
# published set that may not be there -- and the next thing to touch it is a cluster pulling
# the tag. Say so and exit non-zero.
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
echo "  $SIDECAR_TAG"

# The digests are a record and a receipt, not the configuration: one ROBOVAST_PROJECT_TAG
# covers all four members, so it cannot be four different digests. Printed so a release can
# be written down and so a push can be *verified* -- which is what the exit above uses them
# for -- while the two lines below are what actually configures anything.
if [[ ${#DIGEST_REFS[@]} -gt 0 ]]; then
  echo
  echo "Digests (for the record -- what this tag points at right now):"
  printf '  %s\n' "${DIGEST_REFS[@]}"
fi
if [[ ${#MISSING_DIGESTS[@]} -gt 0 ]]; then
  # Only reachable without --push (a --push run with a missing digest exited above). A
  # mixed list is normal there: an image whose rebuild was fully cached still carries the
  # RepoDigest of its earlier push; a rebuilt one is not in the registry at all.
  echo
  echo "Not in ${PROJECT%/} yet (re-run with --push):"
  printf '  %s\n' "${MISSING_DIGESTS[@]}"
fi

echo
echo "Configure it with two lines:"
echo "  ROBOVAST_PROJECT=${PROJECT%/}"
echo "  ROBOVAST_PROJECT_TAG=${TAG}"
if [[ "$TAG" == "latest" ]]; then
  echo
  echo "Note: :latest floats. For a deployment whose image set cannot change under it,"
  echo "re-run with --tag <stamp> (e.g. --tag \"\$(date +%F)\") and pin that instead."
fi
echo "Note: a container's own 'image:' in a .vast is used verbatim and is NOT affected by"
echo "these -- that field is for your own images. Delete it to run a family image."

# Write the two lines into the *user* config -- ~/.config/robovast/env, which
# `vast` loads whatever directory it runs in (see src/robovast/common/env_file.py). The
# per-project ./.env is the wrong home for a released image set: it is one directory's
# setting, and running `vast` one level up silently loses it. Only ever touches these two
# keys in place; any other line (registry credentials, share config) is left untouched.
USER_ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/robovast/env"

set_env_var() {
  local key="$1" value="$2" file="$3"
  mkdir -p "$(dirname "$file")"
  touch "$file"
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    echo "${key}=${value}" >> "$file"
  fi
}

if [[ -n "$CONFIG_WRITE" ]]; then
  set_env_var ROBOVAST_PROJECT "${PROJECT%/}" "$USER_ENV_FILE"
  set_env_var ROBOVAST_PROJECT_TAG "$TAG" "$USER_ENV_FILE"
  echo
  echo "Updated ${USER_ENV_FILE}."
else
  echo
  echo "(nothing written -- re-run with --config-write to put these two lines in ${USER_ENV_FILE})"
fi
