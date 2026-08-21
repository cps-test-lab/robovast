# A publish is a decision, so it is asked out loud rather than remembered as a flag.
#
# Sourced by every script here that can push (release_images.sh, robovast/build.sh,
# controller/build.sh) so the three ask the same question the same way -- three copies of a prompt
# is how one of them grows a default the others do not have, and the one that differs is the one
# that publishes something nobody agreed to.
#
# Contract: the caller sets ASK_PUSH=1 (its `--ask-push` option) and calls `ask_push <what>` before
# building. `--push` still means push, unasked, which is what a non-interactive caller passes.
#
# Two properties this must keep:
#
#   * Asked BEFORE the build. `buildx --push` builds and publishes in one pass, so there is no
#     later moment where the images exist unpublished and the question is still open.
#   * No terminal means no push. A script that inherited one of these commands must not publish
#     because nobody was there to say no -- so the fallback is the same as passing neither flag,
#     and it says so instead of failing.

# ask_push <human-readable description of what would be published>
#
# Sets PUSH=1 if a human says yes. Leaves PUSH untouched otherwise, so the caller's own
# `[[ -n "$PUSH" ]]` checks keep working unchanged.
ask_push() {
  local what="$1" answer

  # Already decided, or the caller did not ask to be asked.
  [[ -n "${PUSH:-}" || -z "${ASK_PUSH:-}" ]] && return 0

  if [[ ! -t 0 ]]; then
    echo "note: not publishing -- --ask-push was given but there is no terminal to ask on." >&2
    echo "      Pass --push to publish without being asked." >&2
    return 0
  fi

  echo "Would publish: $what"
  case "$what" in
    *:latest*)
      echo "  note: 'latest' floats -- every deployment resolving it moves onto this build."
      ;;
  esac
  read -r -p "Push when built? [y/N] " answer
  case "$answer" in
    y|Y|yes|YES) PUSH=1 ;;
    *) echo "Building without publishing. Pass --push to publish." ;;
  esac
}
