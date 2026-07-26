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
previously reached up into ``service.image_build`` for this private constant — the
lone engine-level ``execution → service`` dependency — which inverted the layering.
"""

#: Directory/file names never hashed into or copied as part of a build context.
BUILD_CONTEXT_IGNORE: frozenset[str] = frozenset({
    ".git", "__pycache__", ".cache", ".preprocessed", "results",
    "_execution", "_transient", ".robovast_plugins", "resolved",
})


def render_dockerignore() -> str:
    """:data:`BUILD_CONTEXT_IGNORE` as ``.dockerignore`` patterns.

    The local build hands the project dir to the daemon as-is, so without this the
    whole of ``results/``/``.git``/``.cache`` is transferred on every build. The
    in-cluster path gets the same exclusions by pruning while it stages to S3; this
    is the docker-native equivalent, so the two builders see the same context.

    Each name is emitted twice: bare (a ``.dockerignore`` pattern is anchored at the
    context root) and ``**/``-prefixed, to match the *any path component* rule the
    hashing and staging code applies.
    """
    lines = []
    for name in sorted(BUILD_CONTEXT_IGNORE):
        lines.append(name)
        lines.append(f"**/{name}")
    return "\n".join(lines) + "\n"
