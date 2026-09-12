#!/bin/bash
# Collect the documentation of the packages a campaign is authored against -- the simulator's
# world format and plugin reference, the scenario DSL -- for the controller image to serve
# through `search_docs` (see docs/mcp.rst, "What the documentation corpus covers").
#
# Sourced by container/controller/build.sh and used by the image workflow, so a local build and
# a published one assemble the corpus the same way.
#
# WHERE THE COMMITS COME FROM: the `ARG <NAME>_REPO` / `ARG <NAME>_REF` pairs already in the
# image Dockerfiles. This declares no pin of its own -- a second definition of a ref is free to
# disagree with the first, and the controller would then serve the documentation of a simulator
# other than the one a campaign runs against, which is worse than serving none.
#
# A SIBLING CHECKOUT WINS over the pin. Working on the simulator and the service together, the
# documentation you want in the image is the one on disk; cloning the pin instead would serve
# you last week's page and say nothing about it.
#
# Fail-soft throughout. These are pages: a controller that could not be built because a sibling
# repository was briefly unreachable would trade a complete corpus for no service at all. What
# is missing shows up as fewer pages in `search_docs`, which is a smaller answer, not a wrong one.

#: Read an `ARG NAME=value` default out of a Dockerfile. The pin lives there; this only reads it.
_docs_arg() {
  sed -n "s/^ARG $2=\(.*\)$/\1/p" "$1" | head -1
}

#: Copy the pages -- top-level .rst/.md only, which is exactly what the corpus reads. A whole
#: docs/ is ~17 MB of images and _static against half a megabyte of pages.
_docs_copy() {
  find "$1" -maxdepth 1 -type f \( -name '*.rst' -o -name '*.md' \) -exec cp {} "$2/" \; 2>/dev/null
}

#: How many pages a corpus ended up with. `find | wc -l` rather than `ls`, which shellcheck
#: rightly flags: a filename with a newline in it would make `ls` miscount.
_docs_count() {
  find "$1" -maxdepth 1 -type f | wc -l
}

# substrate_docs_args <repo-root>
# Sets SUBSTRATE_DOCS_ARGS to the --build-context flag, or leaves it empty.
substrate_docs_args() {
  local root="$1"
  local out
  out=$(mktemp -d)
  SUBSTRATE_DOCS_ARGS=()

  local spec label sibling dockerfile repo_arg ref_arg repo ref tmp
  for spec in \
      "roqsim:roqsim:container/robovast/Dockerfile.roqsim:ROQSIM_REPO:ROQSIM_REF" \
      "osc:scenario-execution:container/robovast/Dockerfile:SCENARIO_EXECUTION_REPO:SCENARIO_EXECUTION_REF"
  do
    IFS=: read -r label sibling dockerfile repo_arg ref_arg <<<"$spec"
    mkdir -p "$out/$label"

    if [[ -d "$root/../$sibling/docs" ]]; then
      _docs_copy "$root/../$sibling/docs" "$out/$label"
      echo "substrate docs: $label from the checkout at ../$sibling ($(_docs_count "$out/$label") pages)"
      continue
    fi

    repo=$(_docs_arg "$root/$dockerfile" "$repo_arg")
    ref=$(_docs_arg "$root/$dockerfile" "$ref_arg")
    if [[ -z "$repo" || -z "$ref" ]]; then
      echo "substrate docs: no $repo_arg/$ref_arg in $dockerfile; $label will be missing" >&2
      continue
    fi
    tmp=$(mktemp -d)
    if git clone --quiet "$repo" "$tmp" 2>/dev/null && git -C "$tmp" checkout --quiet "$ref" 2>/dev/null; then
      _docs_copy "$tmp/docs" "$out/$label"
      echo "substrate docs: $label from $repo at ${ref:0:12} ($(_docs_count "$out/$label") pages)"
    else
      echo "substrate docs: could not fetch $repo at '$ref'; $label will be missing" >&2
    fi
    rm -rf "$tmp"
  done

  SUBSTRATE_DOCS_ARGS=(--build-context "substrate-docs=$out")
}
