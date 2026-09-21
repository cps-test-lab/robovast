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

"""Waiting and running subprocesses so a cooperative stop lands while they do.

The counterpart of :class:`~robovast.execution.control_server.ControllerState`'s own
``wait_for_stop``/``raise_if_stopped`` for everything below the execution layer: a
config generator, a plugin installer or a postprocessing plugin holds a ``should_stop``
predicate and knows nothing about campaigns, phases or scopes, which is what keeps those
layers testable without one.

Lives in ``common`` because both the execution layer and config generation call it, and
``common`` may not import execution.
"""

import contextlib
import logging
import os
import signal
import subprocess  # nosec - runs commands the caller already trusts
import threading
import time

from robovast.common.errors import CampaignStopped

logger = logging.getLogger(__name__)

#: How long a terminated process is given to tear itself down before it is killed
#: outright. Long enough for a container runtime's own shutdown -- a term reaches the
#: ``docker`` client, which forwards it to the container, whose entrypoint trap then
#: spends its own timeouts killing and removing it. A shorter wait would escalate to
#: SIGKILL on work that is shutting down correctly, which leaves the container to the
#: daemon's reaping rather than the script's.
DEFAULT_GRACE_S = 15.0

#: How often a stopped-yet? check runs beside a running subprocess. The work watched here
#: is minutes to hours long, so a second's latency is free; polling faster only costs
#: wake-ups on the far more common path where nobody stops anything.
DEFAULT_POLL_S = 1.0


def sleep_unless_stopped(seconds: float, should_stop, poll: float = 0.5) -> bool:
    """Sleep *seconds*, returning early and True as soon as *should_stop* says so.

    What a poll loop below the execution layer uses in place of ``time.sleep``: with no
    predicate to wait on there is no event either, so the sleep is taken in *poll* slices
    and the latency a stop pays is one slice rather than the whole interval. Callers that
    do hold a :class:`~robovast.execution.control_server.ControllerState` use its
    ``wait_for_stop`` instead, which pays nothing at all.
    """
    if should_stop is None:
        time.sleep(seconds)
        return False
    deadline = time.monotonic() + seconds
    while True:
        if should_stop():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll, remaining))


def terminate_group(process: "subprocess.Popen", grace_s: float = DEFAULT_GRACE_S) -> None:
    """Signal *process*'s whole process group, escalating if it does not go.

    The **group**, not the process: bash defers a trap until its foreground child returns,
    so a signal to a wrapper script alone would sit unhandled for as long as the work it is
    waiting on -- which is the entire thing being cancelled. Signalling the group reaches
    the container client too, which forwards it to the container; the script's trap then
    runs and removes it.

    A process that was **not** started in a session of its own shares ours, and signalling
    that group would end the service, the CLI or the test runner that asked for the stop --
    so such a process is signalled alone. Starting the child with ``start_new_session=True``
    is what makes the group reachable, and every caller here that means to kill a group
    does.

    Every failure here is survivable and none is worth raising: the process may have exited
    between the check and the signal, and a stop that cannot be delivered is no reason to
    fail a campaign that is ending anyway.
    """
    try:
        pgid = os.getpgid(process.pid)
    except (OSError, ProcessLookupError):
        return
    own_group = pgid == os.getpgid(0)
    if own_group:
        logger.debug("pid %d shares this process group; signalling it alone", process.pid)
    with contextlib.suppress(OSError, ProcessLookupError, PermissionError):
        if own_group:
            process.terminate()
        else:
            os.killpg(pgid, signal.SIGTERM)
    try:
        process.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        logger.warning("pid %d did not exit after SIGTERM; killing it", process.pid)
        with contextlib.suppress(OSError, ProcessLookupError, PermissionError):
            if own_group:
                process.kill()
            else:
                os.killpg(pgid, signal.SIGKILL)


class StopWatch:
    """Whether the watcher in :func:`watch_stop` was the one that ended the process.

    The one bit a caller cannot recover afterwards: a process killed by a stop and a
    process that failed on its own both come back as a non-zero exit code, and reporting
    the operator's own stop as a fault sends whoever reads it looking for a bug in a step
    that was working.
    """

    def __init__(self):
        self.stopped = False


@contextlib.contextmanager
def watch_stop(should_stop, process: "subprocess.Popen", *,
               poll: float = DEFAULT_POLL_S, grace_s: float = DEFAULT_GRACE_S,
               terminate=None):
    """Kill *process* as soon as *should_stop* says its work is no longer wanted.

    A watching thread rather than a check in the caller's output loop, because that loop is
    blocked in a read on work that prints only now and then -- so a check there would fire
    when the process felt like talking, not when the operator asked it to stop.

    *terminate* is called with the process and replaces the default
    :func:`terminate_group` for work whose group is the wrong handle: an ephemeral
    container is ended by removing it, because a signalled client detaches and leaves the
    container running. Whatever it does, it must leave nothing for the caller to keep
    waiting on -- a caller blocked reading the process's output is only released when the
    process ends.

    A no-op context when no predicate was given, so the ordinary path -- a CLI run, a
    campaign nobody stops -- starts no thread at all. Yields a :class:`StopWatch`.
    """
    watch = StopWatch()
    if should_stop is None:
        yield watch
        return
    done = threading.Event()

    def _watch():
        while not done.wait(poll):
            if should_stop():
                watch.stopped = True
                if terminate is None:
                    terminate_group(process, grace_s)
                else:
                    terminate(process)
                return

    thread = threading.Thread(target=_watch, name="robovast-stop-watch", daemon=True)
    thread.start()
    try:
        yield watch
    finally:
        done.set()


def run_watching_stop(cmd, *, should_stop, stopped_reason: str, on_line=None,
                      poll: float = DEFAULT_POLL_S, grace_s: float = DEFAULT_GRACE_S,
                      **popen_kwargs) -> int:
    """Run *cmd* to completion, raising :class:`CampaignStopped` if a stop ended it.

    For the campaign's own long subprocesses -- the plugin install, the isolated
    composition worker -- where the caller has no use for the exit code of something it
    killed: the stop is the answer, and raising is what carries it past the error handling
    that would otherwise diagnose a signalled process as a broken one. *stopped_reason*
    names the step, since it becomes the campaign's recorded stop message.

    With *on_line*, stdout (stderr merged in) is drained line by line and each stripped
    line handed to it, which is how the callers stream progress. Without it the child's
    streams are left to *popen_kwargs*. The child gets its own session either way, so
    :func:`terminate_group` can reach everything it started.

    Returns the exit code when nothing stopped it -- a caller's own failure diagnosis is
    unaffected.
    """
    stream = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True}
    process = subprocess.Popen(  # nosec - the caller's own trusted command
        cmd, start_new_session=True,
        **({**stream, **popen_kwargs} if on_line is not None else popen_kwargs))
    with watch_stop(should_stop, process, poll=poll, grace_s=grace_s) as watch:
        if on_line is not None:
            for line in process.stdout:
                on_line(line.rstrip("\n"))
        returncode = process.wait()
    if watch.stopped:
        raise CampaignStopped(stopped_reason)
    return returncode
