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

"""Asking the simulator about a campaign's world, on the exec lane's query pool.

Why this exists at all: only the simulator can say whether a world loads and whether its
model compiles, so the question needs a container. ``describe_world_payload`` already
knows how to ask it and how to read a partial answer; what it needs is a *runner*, and
outside a campaign's composition there was none on the cluster lane — which is why
``describe_world`` was refused there outright.

:class:`ExecSlotContainerRunner` is that runner, backed by the same held query container
the read-only introspection tools use. It satisfies the existing ``ContainerRunner``
protocol, so nothing in ``describe_world_payload`` changes: the query construction, the
one-JSON-line reply, ``_command_failure`` and the partial-answer contract all keep
working, and a second simulator backend is served by the same path.

Two lane facts it reconciles, and both are the whole substance of the class:

* **The project is already in the container, at a different address.** The exec lane
  mounts the workspace read-only at ``/sources/<workspace_id>`` (``docker_exec_lane``
  binds it, ``kube_exec_lane`` mirrors it in via an init container), while a backend's
  command names the campaign directory at ``CONFIG_MOUNT``. ``expose`` of a *directory*
  is therefore honoured as a path rewrite rather than a mount: the tree is there, it is
  simply spelled differently.
* **A held container cannot gain a mount.** Mounts are fixed when a container is created,
  and the point of the query pool is that the container outlives the call. So ``expose``
  of a *file* — which in practice is the ``sim`` override document, the one thing argv
  cannot carry — is honoured by writing it in ahead of the command with a heredoc, into
  a writable directory rather than at the path the caller named. That path is an aux
  Pod's declared ``emptyDir``, which this lane's container does not have and, running
  unprivileged, cannot create: staging it verbatim failed on ``mkdir`` before the
  simulator was ever asked, and reported it as a world that does not load.
"""

import logging
import os
import re
import shlex
import tempfile

logger = logging.getLogger(__name__)

#: Where a staged document is written. Writable by an unprivileged container, unlike the
#: mount point a backend names on argv: that is an ``emptyDir`` an aux Pod declares, and the
#: query pool's container is not that Pod. Per-call uniqueness is not needed — the pool
#: serialises a slot's commands, and each writes the document it is about to read.
_STAGE_DIR = "/tmp/robovast-world-query"

#: Heredoc delimiter for a staged document. Long and specific because the document is
#: YAML written by a campaign author: a short delimiter could plausibly occur in it, and
#: a collision would truncate the file silently and leave the simulator describing a world
#: with half its overrides.
_HEREDOC_EOF = "ROQSIM_QUERY_DOCUMENT_EOF"


class QueryCommandFailed(RuntimeError):
    """The command ran and failed. Carries the output so a caller can read the reason."""

    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


class ExecSlotContainerRunner:
    """A ``ContainerRunner`` over one held query container.

    *exec_call* is a one-argument callable taking an ``ExecRequest`` and returning an
    ``ExecResult`` — the transport's own ``exec_in_container``, passed in rather than
    imported so this stays usable from either lane and testable without one.
    """

    def __init__(self, exec_call, *, workspace_id: str, config_path: str,
                 container: str = "simulation"):
        self._exec = exec_call
        self._workspace_id = workspace_id
        self._config_path = config_path
        self._container = container
        #: Where `_stage_query_documents` writes the YAML it wants mounted. A real
        #: directory because that helper opens files in it; nothing here reads it back
        #: except to copy the bytes into the container.
        self.workspace = tempfile.mkdtemp(prefix="robovast_worldq_")
        self._documents: dict = {}
        self._rewrites: dict = {}
        #: The DIRECTORY exposures only, which is what a staged document's *contents* may
        #: be rewritten against. A file exposure maps one exact path, so applying it as a
        #: substring would corrupt any text that happened to contain it.
        self._dir_rewrites: dict = {}
        #: True once the command RAN and exited non-zero — i.e. the simulator reached a
        #: verdict about the world. It is the only thing that separates "your world is
        #: broken" from "I could not ask", and ``describe_world_payload`` raises the same
        #: ``WorldQueryUnavailable`` for both: a world that fails to load prints its reason
        #: and exits 1, exactly like an image that is missing a flag. Without this flag the
        #: check reported an unloadable world as *unverified* and left ``valid: true``,
        #: which is the one outcome worse than not checking at all.
        self.command_failed = False

    # -- ContainerRunner -------------------------------------------------

    def expose(self, host_path: str, container_path: str) -> None:
        """Make *host_path* reachable at *container_path*, without a mount.

        A **directory** becomes a path rewrite: the exec lane already carries the whole
        project, so the tree the caller wants at ``container_path`` is present — under
        ``/sources/<workspace_id>`` instead. Rewriting is what makes a backend's
        ``/config/...`` command address the same file here as it does in a campaign, with
        neither side having to know about the other's mount point.

        A **file** is written in with the command (see :meth:`run`), because a held
        container's mounts were fixed when it was created and this one is meant to outlive
        the call. It lands in :data:`_STAGE_DIR` rather than at *container_path*, which on
        this lane is an aux Pod's mount point that an unprivileged query container may
        neither find nor create; argv is rewritten to the staged path, exactly as it is
        for a directory.
        """
        if os.path.isdir(host_path):
            self._rewrites[container_path.rstrip("/")] = (
                f"/sources/{self._workspace_id}")
            self._dir_rewrites[container_path.rstrip("/")] = (
                f"/sources/{self._workspace_id}")
            return
        staged = f"{_STAGE_DIR}/{os.path.basename(container_path)}"
        with open(host_path, "r", encoding="utf-8") as handle:
            self._documents[staged] = self._rewrite_text(handle.read())
        # Same mechanism as a directory: the file IS reachable, just spelled differently,
        # so argv is rewritten instead of the container being asked for a mount it cannot
        # have. Registered as a rewrite and not only written, or the command would still
        # name the path nothing wrote to.
        self._rewrites[container_path] = staged

    def run(self, command, progress_update_callback=None) -> None:
        """Run *command* in the query container; raise on a non-zero exit.

        Output is fed line by line to *progress_update_callback*, which is how
        ``describe_world_payload`` collects the JSON line it reads the answer from — and
        how it recovers a **partial** answer from a failed run, so the output has to reach
        the caller on failure too, not only on success.
        """
        from robovast.service.interface import ExecRequest

        script = self._script(command)
        result = self._exec(ExecRequest(
            command=script, workspace_id=self._workspace_id,
            config_path=self._config_path, container=self._container,
            # The held pool, never the caller's container: this is a read-only question
            # and a one-shot would destroy whatever they are holding.
            query=True))
        combined = (result.stdout or "") + (result.stderr or "")
        if progress_update_callback is not None:
            for line in combined.splitlines():
                progress_update_callback(line)
        if result.exit_code != 0:
            self.command_failed = True
            raise QueryCommandFailed(
                f"command failed with exit {result.exit_code}", output=combined)

    def close(self) -> None:
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    # -- internals -------------------------------------------------------

    def _script(self, command) -> str:
        """The shell script that stages the documents and then runs *command*."""
        # Newline-separated, never `&&`-joined: a heredoc ends only at a line holding
        # exactly its delimiter, and an `&&` after the terminator leaves that line holding
        # the next command as well. The document then swallowed the describe, the script
        # still exited 0, and every campaign carrying overrides was reported "not checked".
        # `set -e` keeps what the `&&` was there for -- a failed mkdir or cat must not let
        # the simulator go on to read a file nothing wrote.
        parts = ["set -e"]
        for container_path, text in self._documents.items():
            directory = os.path.dirname(container_path)
            if directory:
                parts.append(f"mkdir -p {shlex.quote(directory)}")
            # Quoted delimiter: the document is data and must not be expanded by the
            # shell. An override tree carrying `$` or a backtick would otherwise reach
            # the simulator altered, which is worse than failing.
            parts.append(
                f"cat > {shlex.quote(container_path)} <<'{_HEREDOC_EOF}'\n"
                f"{text.rstrip(chr(10))}\n{_HEREDOC_EOF}")
        parts.append(" ".join(shlex.quote(self._rewrite(arg))
                              for arg in (command or [])))
        return "\n".join(parts)

    def _rewrite_text(self, text: str) -> str:
        """*text* with every exposed directory's container path swapped for this lane's.

        A staged document carries paths the same way argv does, and they need the same
        rewrite -- the override tree is exactly where a campaign names a file that argv
        cannot carry, which is why the document exists at all. Without this the check
        reported a mesh the campaign really does mount as missing, on every configuration
        whose ``sim:`` block names one: the path was correct, and this lane simply spells
        its directory differently.

        Rewritten on a path boundary rather than as a bare substring, so ``/config`` does
        not match inside ``/configuration``.
        """
        for mount, actual in self._dir_rewrites.items():
            text = re.sub(re.escape(mount) + r"""(?=[/\s"'\],}]|$)""", actual, text)
        return text

    def _rewrite(self, arg: str) -> str:
        """*arg* with an exposed directory's container path swapped for this lane's."""
        for mount, actual in self._rewrites.items():
            if arg == mount:
                return actual
            if arg.startswith(mount + "/"):
                return actual + arg[len(mount):]
        return arg


def _problem(message: str, config=None, field: str = "") -> dict:
    """One structured problem, in the shape ``validate_project_file`` returns."""
    return {"stage": "world", "config": config,
            "field": field or "execution.containers.simulation.config",
            "message": message}


def _distinct_blocks(parameters: dict, vast_dir: str) -> list:
    """``[(config_name_or_None, resolved sim block)]``, one per DISTINCT world.

    The campaign default first, then any configuration whose authored ``sim:`` resolves to
    a different block — a campaign may vary its world per configuration, and the one that
    does is precisely where a typo hides in a single cell.

    Deduplicated by the resolved block, because what a world offers depends on the world
    and not on the configuration: describing it once per cell would multiply the cost by
    the sweep for one answer.

    A world produced by a *variation* rather than authored is not reachable here — that
    needs composition, and it is checked there by ``_check_sim_against_world``.
    """
    import json

    from robovast.common.simulators import (backend_name, campaign_sim_block,
                                            flatten_sim_block, merge_sim_block)

    execution = parameters.get("execution", {}) or {}
    if not backend_name(execution):
        return []

    found, seen = [], set()

    def _add(name, block):
        if not block:
            return
        key = json.dumps(block, sort_keys=True, default=str)
        if key in seen:
            return
        seen.add(key)
        found.append((name, block))

    _add(None, campaign_sim_block(execution))
    for config in (parameters.get("configuration") or []):
        if not isinstance(config, dict) or not config.get("sim"):
            continue
        try:
            resolved = merge_sim_block(
                execution, flatten_sim_block(config.get("sim") or {}), vast_dir,
                config_name=config.get("name", ""))
        except Exception as exc:  # noqa: BLE001 - a bad block is the schema's to report
            logger.debug("could not resolve sim block for %s: %s",
                         config.get("name"), exc)
            continue
        _add(config.get("name"), resolved)
    return found


def world_problems(exec_call, *, workspace_id: str, config_path: str,
                   vast_dir: str, parameters: dict) -> list:
    """Does this campaign's world load, and does its model compile?

    One problem per distinct world that does not, in the flat shape the rest of
    ``validate_project`` returns. An empty list means every world was asked and answered
    cleanly — **not** that nothing was checked: a campaign with no simulator backend
    returns early, and anything that could not be asked comes back as an advisory naming
    what would settle it. Silence never stands for a pass.

    Always ``--entities``, i.e. always compiling the model. Measured, the compile adds
    0.1-1.0 s to a container that costs 1-15 s, so the cheaper half-answer buys nothing
    and would leave a broken MJCF invisible until a trial hit it.
    """
    from robovast.common.config_generation import (WorldQueryUnavailable,
                                                   describe_world_payload,
                                                   set_container_runner_factory)
    from robovast.common.errors import ActionableError

    execution = parameters.get("execution", {}) or {}
    problems = []
    for config_name, block in _distinct_blocks(parameters, vast_dir):
        runner = ExecSlotContainerRunner(
            exec_call, workspace_id=workspace_id, config_path=config_path)
        token = set_container_runner_factory(lambda _spec, _r=runner: _r)
        try:
            payload, image = describe_world_payload(
                execution, block, vast_dir, entities=True)
        except WorldQueryUnavailable as exc:
            # One exception, two very different meanings, and only the runner can tell them
            # apart: `describe_world_payload` raises this both when nothing could ask the
            # question AND when the simulator answered by failing. A world that does not
            # load prints its reason and exits 1 -- that is the campaign's own mistake and
            # must fail validation, not be filed as unverified.
            if runner.command_failed:
                problems.append(_problem(
                    f"{block.get('config') or 'this world'} does not load: {exc}",
                    config=config_name))
                continue
            # Genuinely unverifiable from here: no backend, no runner, an image that has to
            # be built first. An advisory, naming what would settle it.
            #
            # One known over-refusal rides in here: describe_world_payload refuses a
            # `build:` image outright, while this lane resolves and would happily run an
            # already-built one. It errs towards "not checked" rather than a wrong pass,
            # so it is left as it is -- lifting it means teaching that function which
            # images this lane can reach.
            step = getattr(exc, "next_step", "")
            problems.append(_problem(
                f"this campaign's world was NOT checked: {exc}."
                + (f" Next: {step}" if step else ""),
                config=config_name))
            continue
        except ActionableError as exc:
            problems.append(_problem(
                f"this campaign's world was NOT checked: {exc}."
                + (f" Next: {exc.next_step}" if exc.next_step else ""),
                config=config_name))
            continue
        finally:
            _reset_factory(token)
            runner.close()

        world = block.get("config") or "this campaign's world"
        errors = (payload or {}).get("errors") or {}
        build_error = errors.get("build")
        if build_error:
            problems.append(_problem(
                f"{world} loads but its model does not compile in {image}: "
                f"{build_error}", config=config_name))
    return problems


def _reset_factory(token) -> None:
    """Undo :func:`set_container_runner_factory`, which hands back a ContextVar ``Token``.

    Restoring the *previous* value rather than clearing: a query may run inside something
    that already installed a factory, and clearing would silently disarm it for whatever
    composes next.
    """
    from robovast.common.config_generation import set_container_runner_factory
    try:
        token.var.reset(token)
    except (AttributeError, LookupError, ValueError):
        # A token from another context cannot be reset here; clearing is then the only
        # safe end state -- leaving this query's runner installed would be worse.
        set_container_runner_factory(None)
