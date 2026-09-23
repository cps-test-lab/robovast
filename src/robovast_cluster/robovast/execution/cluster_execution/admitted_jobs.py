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

"""A set of campaign Jobs the admission queue creates as room appears, tracked to their end.

A batch of scenario runs submits several Jobs under one owner and waits for all of them. The queue (:class:`~.node_admission.AdmissionController`) decides *when* each is
created, by calling the create callback it was given; this module holds the other half --
which of them exist, which are still running, which have finished and must release their
reservation, and which cannot start. Each round is one :meth:`AdmittedJobs.poll`; what a
caller does about a finished or blocked Job is its own business, so the loop around it
stays the caller's.
"""

import dataclasses
import time
from typing import Callable, Dict, List, Optional

from .cluster_execution import (BLOCKED_GRACE_SECONDS, CONTENDED_GRACE_SECONDS,
                                blocked_and_contended_reasons)
from .node_admission import CREATED, PLANNED


@dataclasses.dataclass
class Round:
    """What one :meth:`AdmittedJobs.poll` found."""

    #: Jobs that exist -- created by the queue, or directly where there is no queue.
    created: List[str]
    #: Jobs the queue has not created yet.
    planned: int
    #: Created Jobs still running.
    remaining: List[str]
    #: Created Jobs no longer running. Their reservations are released.
    done: List[str]
    #: ``{job: reason}`` for created Jobs whose pod cannot start, or ``None`` when the pods
    #: could not be read this round -- which is "unknown", never "nothing blocked".
    blocked: Optional[Dict[str, str]]
    #: The blocked Jobs waiting their turn for a node or an image pull, not faulty.
    contended: Dict[str, str]
    #: Jobs that became blocked this round.
    fresh: List[str]
    #: Blocked Jobs past their grace window.
    expired: List[str]
    #: Why the pods could not be read, when :attr:`blocked` is ``None``.
    blocked_error: str = ""

    @property
    def over(self) -> bool:
        """Nothing is running and nothing is left to create."""
        return not self.remaining and not self.planned


class AdmittedJobs:
    """Submit Jobs under one owner and track them round by round.

    *label_selector* selects this owner's Jobs (and their pods) in *namespace*; one listing
    per round answers for all of them, which is what keeps a large batch cheap to watch.
    With no *admission* queue every Job is created at once, unpinned.

    *list_remaining* answers which of a list of created Job names still run, and
    *read_blocked* which of this owner's pods cannot start; they default to
    :func:`running_jobs` and :func:`~.cluster_execution.blocked_and_contended_reasons` over
    *label_selector*.
    """

    def __init__(self, *, admission, owner: str, batch_api, core_api, namespace: str,
                 label_selector: str,
                 blocked_grace: float = BLOCKED_GRACE_SECONDS,
                 contended_grace: float = CONTENDED_GRACE_SECONDS,
                 list_remaining: Optional[Callable] = None,
                 read_blocked: Optional[Callable] = None,
                 clock: Optional[Callable[[], float]] = None):
        self.admission = admission
        self.owner = owner
        self.batch_api = batch_api
        self.core_api = core_api
        self.namespace = namespace
        self.label_selector = label_selector
        self.blocked_grace = blocked_grace
        self.contended_grace = contended_grace
        self._list_remaining = list_remaining or (
            lambda names: running_jobs(batch_api, namespace, label_selector, names))
        self._read_blocked = read_blocked or (
            lambda: blocked_and_contended_reasons(core_api, namespace, label_selector))
        # Looked up per call unless given, so a test that patches the clock is obeyed.
        self._clock = clock or (lambda: time.monotonic())  # pylint: disable=unnecessary-lambda
        # Created directly, where there is no queue to say so.
        self._created: List[str] = []
        #: Per Job, since when its pod could not start. Per Job rather than per set: two
        #: Jobs can be blocked for different reasons at different moments and deserve
        #: different tolerances.
        self.blocked_since: Dict[str, float] = {}

    def submit(self, items, **admission_kwargs) -> None:
        """Hand ``(name, sizing, create)`` items to the queue, or create them all now.

        ``create(node_id=None)`` builds and creates one Job; the queue calls it with the
        node it reserved room on once there is room. *admission_kwargs* are passed to
        :meth:`~.node_admission.AdmissionController.submit`.
        """
        items = list(items)
        if self.admission is None:
            # Unpinned: without a queue nothing has reserved a node, so choosing one here
            # would be a guess the scheduler is better placed to make.
            for name, _sizing, create in items:
                create()
                self._created.append(name)
            return
        self.admission.submit(self.owner, items, **admission_kwargs)

    def adopt(self, names) -> None:
        """Track Jobs that exist already and were never admitted by this tracker.

        A resumed caller meets its earlier attempt's Jobs still running. They hold real
        capacity on a real node, so admitting them again would charge the cluster twice for
        one pod; they are counted as created and waited for.
        """
        self._created.extend(n for n in names if n not in self._created)

    def poll(self, ignore_blocked=()) -> Round:
        """One round: create what has room, then read which Jobs run, finished or wait.

        *ignore_blocked* names Jobs the caller has already dropped: deleting a Job is
        asynchronous, so one keeps reporting itself blocked for a poll or two, and its timer
        must not expire again.
        """
        if self.admission is not None:
            # Works the GLOBAL queue, so this may create another owner's Jobs too -- that is
            # what makes the ordering cluster-wide while keeping the queue thread-free.
            self.admission.drain()
            states = self.admission.states(self.owner)
            created = [n for n, st in states.items() if st == CREATED]
            created += [n for n in self._created if n not in states]
            planned = sum(1 for st in states.values() if st == PLANNED)
        else:
            created, planned = list(self._created), 0
        remaining = self._list_remaining(created)
        still = set(remaining)
        done = [n for n in created if n not in still]
        if self.admission is not None:
            # Release what finished, so the capacity it held is spendable on the next drain.
            for name in done:
                self.admission.finished(name)
        blocked, contended, error = self._blocked(created)
        fresh, expired = self._time_blocked(blocked, contended, set(ignore_blocked))
        return Round(created=created, planned=planned, remaining=remaining, done=done,
                     blocked=blocked, contended=contended, fresh=fresh, expired=expired,
                     blocked_error=error)

    def _blocked(self, created):
        """``(blocked, contended, error)`` among *created*; ``(None, {}, why)`` when
        unreadable."""
        try:
            blocked, contended = self._read_blocked()
        except Exception as exc:  # noqa: BLE001 - "unknown" this round, never "nothing blocked"
            return None, {}, str(exc)
        # CREATED names only, and only this owner's: the selector may be wider than this
        # set, finished Jobs linger for their ttl, and a Job that does not exist cannot be
        # blocked.
        wanted = set(created)
        blocked = {k: v for k, v in blocked.items() if k in wanted}
        contended = {k: v for k, v in contended.items() if k in blocked}
        return blocked, contended, ""

    def _time_blocked(self, blocked, contended, ignore):
        """Advance the per-Job timers; ``(fresh, expired)``."""
        if blocked is None:
            return [], []      # unknown: keep every timer as it is
        if not blocked:
            self.blocked_since.clear()
            return [], []
        now = self._clock()
        fresh = [job for job in blocked if job not in self.blocked_since]
        for job in fresh:
            self.blocked_since[job] = now
        for job in [j for j in self.blocked_since if j not in blocked]:
            del self.blocked_since[job]      # it started after all
        # Two tolerances: a pod waiting its turn for a node or an image pull starts by
        # itself, so it gets the long one; anything else looks the same in ten minutes as
        # in one.
        expired = [job for job, since in self.blocked_since.items()
                   if job not in ignore
                   and now - since >= (self.contended_grace if job in contended
                                       else self.blocked_grace)]
        return fresh, expired


def running_jobs(batch_api, namespace: str, label_selector: str, names,
                 on_status: Optional[Callable] = None) -> List[str]:
    """Which of *names* are still running, in one listing of *label_selector*'s Jobs.

    **Only ever pass names that were actually created.** A name absent from the listing
    counts as finished -- right for a Job that was dropped or garbage-collected, and wrong
    for one not created yet: every planned Job would read as done.

    One ``list`` rather than a status read per name: a large campaign has on the order of a
    thousand Jobs and this runs every couple of seconds. *on_status*, when given, sees each
    listed Job's status as it is read.
    """
    wanted = set(names)
    if not wanted:
        return []
    listing = batch_api.list_namespaced_job(namespace=namespace,
                                            label_selector=label_selector)
    by_name = {j.metadata.name: j for j in listing.items if j.metadata.name in wanted}
    running = []
    for name in names:
        job = by_name.get(name)
        if job is None:
            continue  # finished and garbage-collected, or cleaned up
        status = job.status
        if on_status is not None:
            on_status(name, status)
        if status.active is not None and status.active >= 1:
            running.append(name)
        elif status.completion_time is None and not status.failed:
            running.append(name)
    return running
