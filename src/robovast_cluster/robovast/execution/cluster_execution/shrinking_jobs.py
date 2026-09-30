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

"""The same work over many independent units, in Jobs that shrink when one runs out of memory.

For work that divides into units -- a campaign's runs, whose tables build independently --
and whose memory need is known only by trying: start every Job at one size, and when a Job is
**OOM-killed**, take the rest of that size back and continue one size smaller. The caller says
what a size is (:attr:`ShrinkingJobs.make_items`); this class owns the rest:

* **Generations.** Every Job is submitted under an owner named for its size's level,
  ``<prefix>/g<level>``, the way a campaign's probes queue under ``<campaign>#probes``: taking
  a level back is :meth:`~.node_admission.AdmissionController.cancel` of its owner, which drops
  what the queue has not created and releases what it held, plus deleting its Jobs that run.
* **One kill cancels its level.** Jobs of one level share their size, and their units are
  alike, so the rest of a level that saw a kill is not left to die one by one: it is taken
  back at once, and every unit not yet delivered -- the killed Job's, the cancelled ones', the
  queued ones' -- is submitted again at the next level. Units delivered stay delivered.
* **A floor, never a larger Job.** A level is only ever smaller; memory does not grow, since a
  Job larger than the calibrated size is one the scheduler may not place. When a Job at the
  floor (:attr:`ShrinkingJobs.at_floor`) is killed, the work fails, naming its units: they do
  not fit, and saying so is the only honest end.
* **Other failures are not retried.** A Job that failed for any other reason fails the work
  with that reason; shrinking would only hide it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .admitted_jobs import AdmittedJobs, running_jobs
from .cluster_execution import oom_killed_job_forensics

logger = logging.getLogger(__name__)


class ShrinkingFailed(RuntimeError):
    """The work could not be done: a Job failed, or ran out of memory at the floor."""


@dataclass
class Item:
    """One Job of a level: its name, what it asks the queue for, how to create it, and the
    units it does."""
    name: str
    sizing: object
    create: Callable
    units: Sequence


@dataclass
class _Level:
    level: int
    owner: str
    items: Dict[str, Item]
    tracker: AdmittedJobs
    outcome: Dict[str, str] = field(default_factory=dict)


class ShrinkingJobs:
    """Run *units* in Jobs, shrinking a level at a time on an OOM kill (module docstring).

    *make_items* ``(units, level) -> [Item]`` packs units into the Jobs of *level*: level 0 is
    the starting size and each level is smaller. *at_floor* ``(item, level) -> bool`` says a
    Job cannot be made smaller. *submit_kwargs* go to every
    :meth:`~.admitted_jobs.AdmittedJobs.submit` (``started_at``, ``priority``,
    ``sizing_for_node`` ...), or ``(level) -> dict`` gives each level its own, so the queue
    counts a smaller level at its size; *label_selector* selects this work's Jobs and pods.
    """

    def __init__(self, *, admission, owner_prefix: str, batch_api, core_api, namespace: str,
                 label_selector: str, make_items: Callable[[Sequence, int], List[Item]],
                 at_floor: Callable[[Item, int], bool], submit_kwargs=None,
                 poll_seconds: float = 5.0, sleep: Callable[[float], None] = time.sleep,
                 find_oom: Optional[Callable[[List[str]], set]] = None):
        self.admission = admission
        self.owner_prefix = owner_prefix
        self.batch_api = batch_api
        self.core_api = core_api
        self.namespace = namespace
        self.label_selector = label_selector
        self.make_items = make_items
        self.at_floor = at_floor
        self.submit_kwargs = (submit_kwargs if callable(submit_kwargs)
                              else lambda _level, kw=dict(submit_kwargs or {}): kw)
        self.poll_seconds = poll_seconds
        self.sleep = sleep
        self._find_oom = find_oom or (lambda names: set(oom_killed_job_forensics(
            core_api, namespace, label_selector, job_names=names)))
        #: ``{job name: units}`` of every Job that succeeded, across levels.
        self.delivered: Dict[str, Sequence] = {}
        #: One line per level taken back, for the caller's log.
        self.shrinks: List[str] = []

    def _owner(self, level: int) -> str:
        return f"{self.owner_prefix}/g{level}"

    def _start(self, units: Sequence, level: int) -> _Level:
        items = {item.name: item for item in self.make_items(units, level)}
        owner = self._owner(level)
        state = _Level(level, owner, items, tracker=None)

        def record(name, status):
            if status.succeeded:
                state.outcome[name] = "succeeded"
            elif status.failed:
                state.outcome[name] = "failed"

        state.tracker = AdmittedJobs(
            admission=self.admission, owner=owner, batch_api=self.batch_api,
            core_api=self.core_api, namespace=self.namespace,
            label_selector=self.label_selector,
            list_remaining=lambda names: running_jobs(self.batch_api, self.namespace,
                                                      self.label_selector, names,
                                                      on_status=record))
        state.tracker.submit([(i.name, i.sizing, i.create) for i in items.values()],
                             **self.submit_kwargs(level))
        logger.info("%s level %d: %d job(s) for %d unit(s)", self.owner_prefix, level,
                    len(items), sum(len(i.units) for i in items.values()))
        return state

    def _take_back(self, state: _Level, running: Sequence[str]) -> None:
        """Drop what the queue holds for *state*'s level and delete its running Jobs."""
        if self.admission is not None:
            self.admission.cancel(state.owner)
        for name in running:
            try:
                self.batch_api.delete_namespaced_job(name, self.namespace,
                                                     propagation_policy="Background")
            except Exception as exc:  # noqa: BLE001 - a Job already gone is what was wanted
                logger.debug("could not delete %s: %s", name, exc)

    def run(self, units: Sequence, should_stop: Optional[Callable[[], bool]] = None) -> Dict:
        """Run *units* to the end; ``{job name: units}`` of what was delivered.

        Raises :class:`ShrinkingFailed` when a Job failed, or was OOM-killed at the floor.
        """
        state = self._start(list(units), 0)
        while True:
            if should_stop is not None and should_stop():
                self._take_back(state, list(state.items))
                raise ShrinkingFailed(f"{self.owner_prefix}: stopped")
            rnd = state.tracker.poll()
            if rnd.expired:
                self._take_back(state, rnd.remaining)
                why = "; ".join(f"{n}: {(rnd.blocked or {}).get(n, 'cannot start')}"
                                for n in rnd.expired)
                raise ShrinkingFailed(f"{self.owner_prefix}: job(s) cannot start -- {why}")
            for name in rnd.done:
                if state.outcome.get(name) == "succeeded":
                    self.delivered[name] = state.items[name].units
            failed = [n for n in rnd.done if state.outcome.get(n) == "failed"]
            if failed:
                killed = self._find_oom(failed)
                others = [n for n in failed if n not in killed]
                if others:
                    self._take_back(state, rnd.remaining)
                    raise ShrinkingFailed(
                        f"{self.owner_prefix}: job(s) {', '.join(sorted(others))} failed")
                floor = [n for n in killed if self.at_floor(state.items[n], state.level)]
                if floor:
                    self._take_back(state, rnd.remaining)
                    units_left = [u for n in floor for u in state.items[n].units]
                    raise ShrinkingFailed(
                        f"{self.owner_prefix}: {', '.join(map(str, units_left))} ran out of "
                        "memory at the smallest size; it does not fit in what a job may use")
                self._take_back(state, rnd.remaining)
                left = [u for name, item in state.items.items() if name not in self.delivered
                        for u in item.units]
                self.shrinks.append(
                    f"level {state.level}: {', '.join(sorted(killed))} ran out of memory; "
                    f"{len(left)} unit(s) not delivered go on at level {state.level + 1}")
                logger.warning("%s %s", self.owner_prefix, self.shrinks[-1])
                state = self._start(left, state.level + 1)
                continue
            if rnd.over:
                missing = [n for n in state.items if n not in self.delivered]
                if missing:
                    raise ShrinkingFailed(f"{self.owner_prefix}: job(s) {', '.join(missing)} "
                                          "ended without succeeding")
                return self.delivered
            self.sleep(self.poll_seconds)


__all__ = ["Item", "ShrinkingFailed", "ShrinkingJobs"]
