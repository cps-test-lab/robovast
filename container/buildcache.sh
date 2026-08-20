# Registry-backed layer cache for the local release builds.
#
# Sourced by container/robovast/build.sh, container/controller/build.sh and
# container/release_images.sh -- one definition, for the reason platforms.env gives: encoding
# it per script is how they drift.
#
# This is the mechanism the in-cluster image builds have always used
# (robovast/execution/cluster_execution/cluster_image_build.py, cache_image_ref) and the local
# release path did not. CI has its own cache (type=gha); locally there was none, so the only
# layer reuse was whatever one machine's builder happened to still hold -- and a prune, a
# builder recreate, a full disk or a second machine each meant a genuinely cold rebuild of the
# ROS base, both colcon workspaces and every pip pass.
#
# Two decisions copied from cache_image_ref, both load-bearing:
#
#   The cache ref is NOT tag-qualified. Its whole purpose is for the build of tag B to import
#   the layers produced for tag A, so `:buildcache` is shared across every tag of that repo.
#   Qualifying it would give each release its own empty cache.
#
#   Export failures are ignored (ignore-error=true). By the time the cache is written the image
#   itself has already been pushed, so a read-only or full registry must not turn a successful
#   release into a failure.
#
# Import and export are NOT symmetric, and that is a driver constraint rather than a choice:
#   --cache-from  both drivers accept, so a local --load build imports too and a fresh machine
#                 starts warm.
#   --cache-to    only the docker-container driver can do. The `docker` driver refuses outright
#                 ("Cache export is not supported for the docker driver"), and build.sh pins
#                 that driver for local --load builds so the daemon's images stay visible. So
#                 export is attached only to a --push, which is the container driver's path.
# Adding --cache-to to the local path would fail every local build; this asymmetry is the fix,
# not an oversight.
#
# Set RELEASE_BUILD_CACHE=0 to turn both off -- a metered connection, an offline build, or a
# registry that should not carry cache tags.

# buildcache_args <published_image_ref> [push]
#
# Sets BUILDCACHE_ARGS to the flags for that image's cache, or to nothing. Pass a non-empty
# second argument when the build pushes.
buildcache_args() {
  local ref="$1" push="${2:-}" repo
  BUILDCACHE_ARGS=()

  if [[ "${RELEASE_BUILD_CACHE:-1}" == "0" ]]; then
    return 0
  fi

  # Strip the tag, not a fixed ":latest": only the last colon-suffix goes, so a registry port
  # (host:5000/ns/img:tag) survives.
  repo="${ref%:*}"

  # No registry in the ref means a Docker Hub library name -- a plain local build with no
  # --project. A `:buildcache` tag there is not ours to write and is not what any release
  # means, so there is no cache rather than a wrong one.
  if [[ "$repo" != */* ]]; then
    return 0
  fi

  BUILDCACHE_ARGS=(--cache-from "type=registry,ref=${repo}:buildcache")
  if [[ -n "$push" ]]; then
    BUILDCACHE_ARGS+=(--cache-to "type=registry,ref=${repo}:buildcache,mode=max,ignore-error=true")
  fi

  # Explicit, and not an `[[ ... ]] && ...` last line: that form makes the test's result the
  # function's exit status, so a false test returns 1 and every caller's `|| return $?` aborts
  # before docker is invoked. build.sh carries a comment about the build that silently did
  # nothing for exactly this reason.
  return 0
}
