# What a locally built image reports about its own build: which revision, and when.
#
# Sourced by container/robovast/build.sh and container/controller/build.sh so both bake the
# same values the same way -- for the reason platforms.env and buildcache.sh give, and with the
# gap this file closes as the evidence: CI passed ROBOVAST_GIT_REVISION and the local build
# scripts did not, so every locally released family deployed a service that could not say which
# code it ran. `get_service_info` then reported no revision at all, and a caller asking "is my
# change loaded?" had nothing to compare and had to probe for the behaviour instead.
#
# Derived, never passed. There is deliberately no --revision or --date option: an option is
# something to forget, and forgetting it is exactly the bug this fixes. Which is also why both
# stamps live here rather than one of them being a line in whichever build script needed it
# first -- a second stamp wired into one script and not the other is the same bug again.
#
# The revision must be byte-identical to what `robovast.common.execution._git_revision()` reports
# for the same checkout -- short sha, plus `+dirty` from a plain `git status --porcelain`
# (untracked included) -- because the whole feature is a string comparison between the two.
# tests/common/test_release_bakes_revision.py runs this helper and asserts that equality; it is
# not a style check, it is the only thing keeping two implementations of one formula in step.
#
# Empty is a real answer. A build outside a git checkout (an exported tree, a tarball) bakes
# no revision, and `code_revision()` then reports "" -- "this deployment cannot tell you", which
# a caller can act on. A fabricated value would instead read as a revision that merely differs.

# image_stamp_args [repo_root]
#
# Sets GIT_REVISION_ARGS, BUILD_DATE_ARGS and COMPAT_VERSION_ARGS to the --build-arg pairs for
# that checkout. The first two may be empty; the last may not -- see below.
#
# Defaults to the repository this file lives in, which is the one whose code goes into
# the image -- not the caller's working directory, which may be a superproject holding robovast
# as a submodule.
#
# Two arrays and not one: a build script expands only the arrays its Dockerfile declares an ARG
# for, and passing a --build-arg no Dockerfile declares is a warning on every build.
image_stamp_args() {
  local root="${1:-}" sha dirty compat
  GIT_REVISION_ARGS=()
  COMPAT_VERSION_ARGS=()

  if [[ -z "$root" ]]; then
    root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
  fi

  # Wall-clock at build time, which is the only moment it is knowable: an image cannot read its
  # own labels from inside, so a service answering "how old is the code I am running?" needs the
  # answer baked in, exactly as the revision is. RFC 3339 in UTC, matching the format
  # docker/metadata-action puts in org.opencontainers.image.created, so the CI-built and the
  # locally built image are read the same way. Unlike the revision this changes on every build
  # rather than every commit, so its ARG belongs at the very end of a Dockerfile.
  BUILD_DATE_ARGS=(--build-arg "ROBOVAST_BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)")

  # The host<->container protocol version, read from the one place that defines it instead of
  # being written into each Dockerfile by hand. It used to be a literal in three files kept in
  # step by a CI gate -- which is a gate precisely because a hand-copied constant drifts.
  # Derived like the two above, it cannot.
  #
  # FATAL when it cannot be read, unlike an absent revision. An empty revision is a real answer
  # ("this deployment cannot tell you"); an empty protocol version produces an image whose label
  # is the empty string, which every host then refuses to drive. Better to fail the build.
  compat=$(grep -oP '^COMPAT_VERSION\s*=\s*\K\d+' "$root/src/robovast/common/execution.py" \
           2>/dev/null || true)
  if [[ -z "$compat" ]]; then
    echo "error: could not read COMPAT_VERSION from $root/src/robovast/common/execution.py." >&2
    echo "       The image would carry an empty org.robovast.compat-version label, and every" >&2
    echo "       host would refuse to drive it. Not building." >&2
    return 1
  fi
  COMPAT_VERSION_ARGS=(--build-arg "ROBOVAST_COMPAT_VERSION=$compat")

  # Both reads, or neither: a sha without knowing whether the tree is clean would be baked as
  # if it described the code, and a dirty tree's sha does not.
  if ! sha=$(git -C "$root" rev-parse --short HEAD 2>/dev/null) \
     || ! dirty=$(git -C "$root" status --porcelain 2>/dev/null); then
    echo "note: $root is not a git checkout (or git is unavailable), so this image will report" >&2
    echo "      no revision -- a service deployed from it cannot say which code it runs." >&2
    return 0
  fi

  [[ -n "$dirty" ]] && sha="${sha}+dirty"
  GIT_REVISION_ARGS=(--build-arg "ROBOVAST_GIT_REVISION=$sha")

  # Said out loud, like a --roqsim-src build says so: the image no longer corresponds to a
  # commit anyone else can check out, and the one line of build output is where that is
  # noticeable.
  echo "Baking revision:  $sha"
  if [[ "$sha" == *+dirty ]]; then
    echo "  the tree has uncommitted changes, so this image is not reproducible from a commit;"
    echo "  campaigns run against it record that fact rather than looking clean."
  fi
}
