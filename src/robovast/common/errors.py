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

import errno
import os
import sqlite3


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


class CampaignStopped(Exception):
    """Raised when a batch is abandoned because a cooperative stop was requested.

    A *clean* terminal signal (Ctrl+C on ``vast serve``, the Stop button, an MCP
    stop) — distinct from a genuine failure. Callers set the campaign phase to
    ``"stopped"`` and skip the finish work that would otherwise fail noisily against a
    torn-down cluster tunnel and produce misleading tracebacks. The analysis of the
    batches that did finish is *not* part of what is skipped — it is owed, and the
    service runs it (``ServiceBase._launch_campaign``'s stopped path).
    """


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


class ExecPathUnavailable(RuntimeError):
    """Raised when no command can be run in a container here at all.

    A property of the *deployment*, never of the image, the command or the project: an
    upgrade request answered with an ordinary HTTP success means nothing serving it upgraded
    the connection, so every exec is refused equally, before the command exists. The pod and
    container one attempt named are therefore incidental, and naming them invites a caller
    to try another.

    A status that is *not* a success is not this: the API server answering ``404`` or
    ``500`` is answering about the one target that was asked for, which a caller may retry
    against another.

    Distinct from a command that ran and failed, which is the caller's own question
    answered, and from :class:`ClusterUnreachableError`, where the API server never answered
    at all. Here it answers: everything that needs no container keeps working, which is what
    makes the consequence worth stating rather than leaving to be inferred.

    Its own type because the callers that must degrade rather than mis-attribute recognise
    it structurally -- the world and scenario checks report *unchecked*, the image catalogs
    report the deployment instead of the image. Across HTTP the type is carried as the
    :data:`~robovast.service.interface.EXEC_PATH_UNAVAILABLE` code on the refusal, so a
    client recognises the same fact without matching on the message.

    A ``RuntimeError`` so the callers that already catch one keep working; the service
    maps this subclass to 503 rather than 409.
    """

    include_traceback = False


class ExecTargetGone(RuntimeError):
    """Raised when the pod or container an exec named is no longer there.

    The other half of the read :class:`ExecPathUnavailable` describes: the API server
    answered about *the one target that was asked for* rather than refusing every exec, so
    the deployment needs no attention and the target can simply be made again. A pod ends
    without its span ending -- an eviction, a drained node, a deadline -- and the next exec
    into it is the first thing that notices.

    Its own type because the caller that can act on it cannot act on a message: a runner
    holding a name that no longer resolves recreates the container and repeats what it was
    doing, which is only correct for *this* cause. A command that ran and failed, or a
    deployment that can exec nothing, must not be retried that way.

    A ``RuntimeError`` for the reason its siblings are: callers that already catch one keep
    working unchanged, and only the one that knows how to recover matches the subclass.
    """


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
    """Raised when a container's ``build:`` image is not in the deployment's own image store.

    Never built implicitly: a diagnostic exec that quietly became a multi-minute image
    build would answer a question nobody asked. What separates this from a dead end is the
    *state* — no build known, one running, one failed, or one that succeeded and whose
    image has since gone — and :func:`~robovast.service.image_build.not_built_message`
    turns each into a different :attr:`ActionableError.next_step`.
    """


class AuxContainerUnavailable(ActionableError):
    """A variation needs an auxiliary container and nothing can provide one here.

    A runner for a *variation's* helper image is arranged **per span** by whoever is about
    to compose (``ServiceBase._aux_runner_context``): a campaign gets one for the run and
    a preview gets one held by the exec manager. This is raised when a composition
    reached a variation that wants one and neither applied -- a process with no backend,
    or a caller that composed without arranging anything.

    So the reason is always the *caller's context*, never the ``.vast``: the same file
    composes wherever a runner is arranged. A runner is **not** confined to a campaign's
    composition -- the scene cache opens an aux pod outside one for its cache fills -- and
    reading
    it as a rule is what left ``preview_configurations`` refusing a perfectly good sweep.

    What this does *not* extend to is the exec manager's **query slot**: that runs a read-only
    question in a campaign's own image with nothing written back, so it is not a substitute
    for a helper image a variation writes into. A held *aux* slot in the same manager is,
    and is how a preview gets one.

    Before this refusal existed the composition path fell through to the local ``docker
    run`` and died in ``Popen`` with a bare ``FileNotFoundError: 'docker'`` -- which reads
    as a broken ``.vast`` rather than as a runner that was never arranged, and names neither
    the variation nor the container it wanted.

    Deliberately *conditional*: on a host that has ``docker`` the local fallback genuinely
    works, and composing a container-backed variation there must keep working.
    """


class ImageStoreUnavailable(RuntimeError):
    """Raised when an image store could not be asked whether an image is there.

    "I could not check" and "it is not there" are different answers, and conflating them
    is a bug this class exists to prevent: a local store that swallows a missing docker CLI
    into ``image_exists() -> False`` makes a service running where no docker daemon exists
    report every built image as unbuilt — a missing *dependency* reported as a missing
    *artifact*.

    A ``RuntimeError`` so the readers that already degrade on one keep working unchanged.
    """

    include_traceback = False


class InsufficientStorageError(ActionableError):
    """Raised when a write is refused because free space is below the reserve.

    Distinct from a write that already failed for lack of space (:func:`is_storage_full`):
    this is RoboVAST declining new work while it still has room to keep running what it has
    (see :mod:`robovast.common.disk_reserve`). Both reach an HTTP caller as a 507; this one
    says which disk is short and by how much, and carries clearing the service cache as its
    ``next_step`` when that would free something worth it.

    Not a ``RuntimeError``: nothing is in conflict, and a caller that maps a conflict to
    "wait for the other operation" would wait for something that will not finish.
    """


#: What a caller is told when a write failed because the service's storage is full. One
#: sentence for every surface, and no path: where on the service host the write landed is
#: nothing a caller can act on.
STORAGE_FULL_DETAIL = (
    "The service ran out of disk space and did not complete this request. The request "
    "itself is fine: free space on the service's storage -- deleting campaigns that are "
    "no longer needed is the usual way -- then retry.")

#: SQLite's primary result code for a full disk: what a campaign's ``campaign.db`` answers
#: when the results volume is full.
_SQLITE_FULL = sqlite3.SQLITE_FULL


def is_storage_full(exc: BaseException) -> bool:
    """Whether *exc*, or anything that caused it, says the storage behind a write is full.

    Two shapes carry that fact: the kernel's ``ENOSPC``/``EDQUOT`` on a file write, and
    SQLite's ``SQLITE_FULL`` on a write to a campaign's store, which SQLite raises as its own
    error rather than the ``OSError`` behind it.

    The cause chain is followed because a layer that translates an ``OSError`` into its own
    refusal (an archive that could not be extracted is a ``ValueError``) would otherwise
    turn "the disk is full" into "your input is wrong", which sends the caller to fix a
    request that was never the problem.
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
            return True
        if getattr(exc, "sqlite_errorcode", None) == _SQLITE_FULL:
            return True
        # The chain a traceback prints: an explicit cause, else the exception being handled
        # when this one was raised -- unless ``from None`` said that one is irrelevant.
        if exc.__cause__ is not None:
            exc = exc.__cause__
        else:
            exc = None if exc.__suppress_context__ else exc.__context__
    return False


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
