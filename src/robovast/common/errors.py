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

"""Error types shared by the campaign pipeline.

Lives in ``common`` so config generation and campaign staging can raise the same
user-error type the execution backends do, without importing the execution layer
(which imports *them*).
"""

import os


class CampaignConfigError(Exception):
    """Raised when the campaign cannot start because of bad user input.

    A typo'd ``--config`` filter, an empty vast-file, a ``.vast`` pointing at a
    file that is not there — a user error, not a bug. The message is
    self-contained and actionable (it names the offending ``.vast`` key and what
    to do), so callers surface it as ``phase=failed`` *without* an accompanying
    stack trace, which would only be noise here.
    """

    # Read by failure_detail(): a clean user error carries no traceback into the
    # durable failure record, matching how the worker already logs it.
    include_traceback = False


class ClusterUnreachableError(Exception):
    """Raised when the Kubernetes API server cannot be reached at all.

    A stopped cluster, a down VPN, a kubeconfig pointing at an endpoint that no
    longer answers: the request never gets a reply, so there is no API answer to
    interpret. Distinct from :class:`CampaignConfigError` (the cluster answered and
    the configuration is wrong) and from ``KueueCheckUnavailable`` (the cluster
    answered "forbidden").

    Like a config error it is self-contained and actionable — the stack through
    urllib3's retry machinery names no cause the message does not — so it carries no
    traceback into the log or the durable failure record.
    """

    include_traceback = False


class ImageBuildFailed(RuntimeError):
    """Raised when a campaign's experiment image did not build.

    The builder's own output is the diagnosis, and ``classify_build_error`` has
    already reduced it to one actionable line (which apt/pip entry, or which
    server-side knob) plus a pointer to the campaign's BUILD log. The Python stack
    is the wait loop and names nothing the message does not, so — like a config
    error — this carries no traceback.

    A ``RuntimeError`` so callers that predate the class still catch it.
    """

    include_traceback = False


def missing_input_error(entries, *, hint=True):
    """Build a :class:`CampaignConfigError` for missing project input files.

    *entries* is a sequence of ``(key, referenced, resolved)`` triples: the
    ``.vast`` key (or config name) the path came from, the path as written there,
    and the absolute path it resolved to. All missing inputs are reported in one
    error rather than one-per-attempt, so a user fixing paths sees the whole list
    instead of rediscovering it file by file.
    """
    lines = ["The campaign references files that do not exist:"]
    for key, referenced, resolved in entries:
        lines.append(f"  {key}: {referenced if referenced else '(not set)'}")
        # Only worth a second line when resolution actually moved the path — for an
        # entry already written as an absolute path it would just repeat it.
        if resolved and os.path.abspath(str(referenced)) != os.path.abspath(str(resolved)):
            lines.append(f"    resolved to: {resolved}")
    if hint:
        lines.append("Fix the paths in the .vast file (they are resolved relative "
                     "to the .vast's own directory) or add the missing files; "
                     "'vast config validate' checks them without starting a run.")
    return CampaignConfigError("\n".join(lines))
