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
    the configuration is wrong) and from a check that could not be run (the cluster
    answered "forbidden").

    Like a config error it is self-contained and actionable — the stack through
    urllib3's retry machinery names no cause the message does not — so it carries no
    traceback into the log or the durable failure record.
    """

    include_traceback = False


class ObjectStoreUnreachableError(RuntimeError):
    """Raised when the campaign object store did not answer at all.

    A dropped or stalled ``kubectl port-forward``, a MinIO pod that went away, a
    connection reset mid-response: botocore reports each of these as a different
    transport exception, and every one of them means the same thing — no answer, so
    there is nothing to interpret. Left raw they reach the caller as a ~90-line
    traceback through urllib3, botocore's retry handler and the ASGI stack that names
    no cause the one sentence here does not.

    Distinct from a ``ClientError``: the store answered, and *what* it answered
    (``NoSuchBucket``, ``NoSuchKey``) is the caller's question to interpret.

    A ``RuntimeError`` so that the readers which already degrade on one
    (``_campaign_records`` falling back to "unknown", the service's ``_guard``) keep
    working unchanged; the service maps this subclass to 503 rather than 409.
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


class ActionableError(Exception):
    """An error that knows the one command that would move the caller forward.

    The MCP surface already hands back a ``next_step`` on the paths that *succeed*
    (``start_campaign``, ``build_experiment_image``) precisely because an answer carrying
    only an id leaves "and now wait for it" to be remembered. A refusal is where the next
    action is least obvious and most needed, and nothing carried one — so the hint rides on
    the exception, and the MCP layer surfaces it beside ``error`` for whichever tool raised.

    *next_step* is a literal command or tool call with the ids already filled in, or empty
    when there is genuinely nothing obvious to do next. Empty is a real answer: a hint on
    every reply is a field callers learn to skip.
    """

    include_traceback = False

    def __init__(self, message: str, next_step: str = ""):
        super().__init__(message)
        self.next_step = next_step


class ImageNotBuilt(ActionableError):
    """Raised when a container's ``build:`` image is not on the lane's own image store.

    Never built implicitly: a diagnostic exec that quietly became a multi-minute image
    build would answer a question nobody asked. What separates this from a dead end is the
    *state* — no build known, one running, one failed, or one that succeeded and whose
    image has since gone — and :func:`~robovast.service.image_build.not_built_message`
    turns each into a different :attr:`ActionableError.next_step`.
    """


class AuxContainerUnavailable(ActionableError):
    """A variation needs an auxiliary container and nothing can provide one here.

    A container runner for a *variation's* helper image exists only inside a campaign's
    composition: the cluster lane installs a per-campaign aux-pod factory, and the local
    lane falls back to ``docker`` on the service host. Anything composing **outside** a
    campaign -- ``validate_project``, ``preview_configurations``, a scene or screenshot
    query -- has neither.

    Deliberately still true after the world check moved onto the exec lane's query pool
    (``service/world_query.py``): that pool runs a *read-only question* in a container the
    service already knows how to start, while a variation's aux container is a helper image
    the variation writes into. The second is what has no runner here, and merging the two
    would hand a variation a container it cannot use as one.

    What was missing is that the *composition* path never refused. It fell through to the
    local ``docker run`` fallback and died in ``Popen`` with a bare ``FileNotFoundError:
    'docker'`` -- which reads as a broken ``.vast`` rather than as a runner that was never
    wired, and names neither the variation nor the container it wanted. This is that
    refusal, with the :attr:`~ActionableError.next_step` that says what would exercise it
    and what that costs.

    Deliberately *conditional*: on a host that has ``docker`` the local fallback genuinely
    works, and previewing a container-backed variation there must keep working.
    """


class ImageStoreUnavailable(RuntimeError):
    """Raised when an image store could not be asked whether an image is there.

    "I could not check" and "it is not there" are different answers, and conflating them
    is a bug this class exists to prevent: the local store used to swallow a missing docker
    CLI into ``image_exists() -> False``, so a service running where no docker daemon
    exists reported every built image as unbuilt — a missing *dependency* reported as a
    missing *artifact*, which cost a real investigation.

    A ``RuntimeError`` for the same reason :class:`ObjectStoreUnreachableError` is one: the
    readers that already degrade on one keep working unchanged.
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
