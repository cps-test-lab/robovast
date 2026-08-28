# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Shared build-context staging rules.

The set of directory/file names that are never hashed into or copied as part of a
project's docker build context. Both the local build path
(:mod:`robovast.service.image_build`) and the in-cluster BuildKit staging
(:mod:`robovast.execution.cluster_execution.cluster_image_build`) must skip the
*same* heavy/irrelevant paths, or a context hash computed on one side would not
match the tree staged on the other.

It lives in ``common`` so both sides import it *downward*: the cluster build code
reaching up into ``service.image_build`` for this constant would be an engine-level
``execution → service`` dependency, which inverts the layering.
"""

from pathlib import Path

#: Directory/file names never hashed into or copied as part of a build context.
#:
#: The second line is Python build output, and it is excluded for correctness rather than
#: for weight. A container block may now install a source directory
#: (``python_packages: [./]``) instead of a prebuilt wheel, which means setuptools runs
#: *inside* the image build -- and setuptools reuses an existing ``build/lib`` in
#: preference to the sources beside it. A stale one left by an earlier local
#: ``pip install`` therefore gets baked into the campaign image, silently, and the run
#: executes code the working tree no longer contains. That is a wrong result reported as
#: a passing run, which is the worst failure this pipeline can produce.
#:
#: ``.venv`` is here for both reasons: staging one costs hundreds of megabytes, and a
#: virtualenv's absolute shebangs are meaningless in the image anyway.
BUILD_CONTEXT_IGNORE: frozenset[str] = frozenset({
    ".git", "__pycache__", ".cache", ".preprocessed", "results",
    "_execution", "_transient", ".robovast_plugins", "resolved",
    "build", "dist", ".eggs", ".venv", ".tox", ".pytest_cache",
    ".mypy_cache", ".ruff_cache",
})


#: The child that marks a directory as a campaign's output rather than project source.
#: Every campaign writes one; nothing a project authors does.
_CAMPAIGN_MARKER = "_execution"


def is_campaign_output(path) -> bool:
    """Is *path* a downloaded campaign's directory?

    Answered by **structure** -- it holds an ``_execution/`` child -- rather than by a name
    pattern. A campaign directory is named after its campaign id, so there is no fixed name
    to list, and the pattern that would match one is the pattern a project happens to use
    for its campaigns today. ``results/`` was ignored and this was not, so a
    ``--wait-and-download`` that lands its output beside the sources (rather than under
    ``results/``) put the whole campaign -- rosbags included -- into every build context
    from then on. One project was staging 592 MB of them around a 15 MB tree, on every
    build of every container, and nothing said so.

    Structure also means this cannot go stale: a campaign directory is recognised whatever
    it is called, in a project nobody thought about when this was written.
    """
    path = Path(path)
    return path.is_dir() and (path / _CAMPAIGN_MARKER).is_dir()


def campaign_outputs_in(root) -> list:
    """Campaign directories under *root*, as paths relative to it.

    Not recursive into a campaign once found (its insides are all output), and never
    descends into an already-ignored name -- so this walks the project, not the results
    it is trying to skip.
    """
    root = Path(root)
    found, stack = [], [root]
    while stack:
        current = stack.pop()
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for child in children:
            # Symlinked directories are not descended into: a link back to an ancestor
            # would make this walk forever, and docker does not follow one into a build
            # context either, so there is nothing behind it to find.
            if child.is_symlink() or not child.is_dir():
                continue
            if child.name in BUILD_CONTEXT_IGNORE:
                continue
            if is_campaign_output(child):
                found.append(child.relative_to(root))
            else:
                stack.append(child)
    return sorted(found)


def render_dockerignore(project_dir=None) -> str:
    """:data:`BUILD_CONTEXT_IGNORE` as ``.dockerignore`` patterns.

    The local build hands the project dir to the daemon as-is, so without this the
    whole of ``results/``/``.git``/``.cache`` is transferred on every build. The
    in-cluster path gets the same exclusions by pruning while it stages to S3; this
    is the docker-native equivalent, so the two builders see the same context.

    Each name is emitted twice: bare (a ``.dockerignore`` pattern is anchored at the
    context root) and ``**/``-prefixed, to match the *any path component* rule the
    hashing and staging code applies.

    *project_dir* adds the campaign outputs found under it (:func:`campaign_outputs_in`).
    They have to be listed by path because they are recognised by structure and a
    ``.dockerignore`` cannot express "a directory containing ``_execution/``" -- so this
    is the one part of the two lanes' agreement that is computed rather than declared.
    Omitting it yields the static set alone, which is a *larger* context and never a
    wrong one.
    """
    lines = []
    for name in sorted(BUILD_CONTEXT_IGNORE):
        lines.append(name)
        lines.append(f"**/{name}")
    if project_dir is not None:
        for rel in campaign_outputs_in(project_dir):
            # A .dockerignore pattern is always '/'-separated, whatever the host uses.
            lines.append(rel.as_posix())
    return "\n".join(lines) + "\n"
