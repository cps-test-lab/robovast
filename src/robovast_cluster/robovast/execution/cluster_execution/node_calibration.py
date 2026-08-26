# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Per-node container sizing, learned from one discarded run per node.

**Why per node at all.** The same trial costs about 1.6x more CPU on the slowest machine of a
mixed cluster than on the fastest, and wall time does not show it -- a realtime-paced
simulator holds one simulated second per wall second, so every machine finishes at roughly
the same time and the difference lands entirely in CPU consumed. One declared number is
therefore wrong on every node but the one it was measured on: sized for the fast node it
starves the slow one, sized for the slow node it wastes the fast one.

**And it is a validity matter, not only a throughput one.** Measured on 2026-08-26 at a
uniform 3.0 cores for the system under test: the Xeon was quota-bound in 100% of its runs at
2.5 and below, while the other three nodes were never quota-bound at any allocation down to
2.0. Equal *cores* are not equal *compute*, so an equal declaration produces unequal
conditions -- which is the thing a uniform number was supposed to prevent. Sizing each node
so the stack meets its deadlines everywhere equalises the behaviour instead of the
accounting.

**How the figure is found: one run per node, discarded.**

The first job placed on a node runs at the **declared** size and is a *calibration run*. What
it measured becomes that node's figures for the rest of the campaign, and the run itself is
dropped from the results -- it was executed under a different allocation from every other run
on that node, so keeping it would put exactly the inconsistency this exists to remove into
the data.

Frozen once set. Continuing to adapt would mean run 5 and run 40 on the same node ran in
different environments, which is the same defect in a slower form.

**Pilots calibrate nothing.** When no node would receive a second run there is nothing for a
calibration run to pay for -- discarding one would discard the campaign. The rule needs no
tuned constant: if the plan has no more jobs than the cluster has nodes, no node gets a
second one, so the whole mechanism is skipped and the campaign behaves exactly as it did
before any of this existed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Headroom over what a calibration run was measured using. The same figure ``advice.py``
#: applies when it suggests a reservation, and for the same reason: a measured peak is one
#: sample of a distribution, and sizing exactly at it guarantees the next run is clipped.
CALIBRATION_HEADROOM = 1.25

#: Never size a container below this, whatever it measured. A container that happened to do
#: very little in its calibration run -- a trial that failed early, a simulator that never got
#: past bring-up -- would otherwise pin the node to a figure the next run cannot live in.
MIN_CPU = 0.25


@dataclass
class NodeCalibration:
    """Per-node container CPU, learned once per node per campaign.

    Deliberately in-memory and per campaign. A figure cached across campaigns would be exactly
    the transferable factor this cluster's own data refuted: measured against two unlike
    campaigns, container rankings *invert* between nodes and per-``(node, container)`` costs
    move up to 40%, so a number learned in one campaign mis-sizes the next. Measuring this
    campaign, on this node, under the contention this campaign actually meets is the only
    model the data supports -- and it is why the cost is one run per node rather than a
    benchmark suite.
    """

    #: node_id -> {container_name: cpu_cores}
    _by_node: dict = field(default_factory=dict)
    #: node_id -> the job key whose run is paying for that node's figures
    _probes: dict = field(default_factory=dict)
    #: job keys whose results must not be kept
    _discard: set = field(default_factory=set)
    enabled: bool = True

    def calibrated(self, node_id) -> "dict | None":
        """That node's per-container cores, or ``None`` while it is still unknown."""
        return self._by_node.get(node_id)

    def claim_probe(self, node_id, job_key) -> bool:
        """Mark *job_key* as the calibration run for *node_id*, if one is not already out.

        **At most one outstanding per node**, which is the whole reason this is a claim rather
        than a flag. Without it the first wave of a batch lands several jobs on a node before
        any of them has finished, every one of them is a probe, and every one is discarded --
        the campaign pays for its calibration several times over and loses the runs.
        """
        if not self.enabled or node_id is None or node_id in self._by_node:
            return False
        if node_id in self._probes:
            return False
        self._probes[node_id] = job_key
        self._discard.add(job_key)
        return True

    def record(self, node_id, job_key, measured: dict) -> bool:
        """Take a finished probe's per-container cores as that node's figures.

        *measured* is ``{container: peak_cores}``. Returns whether anything was stored: a
        probe that produced no measurement leaves the node uncalibrated, so the next job there
        becomes the probe instead. Silence is not a measurement of zero.
        """
        if self._probes.get(node_id) != job_key:
            return False
        self._probes.pop(node_id, None)
        figures = {name: max(MIN_CPU, round(cores * CALIBRATION_HEADROOM, 3))
                   for name, cores in (measured or {}).items() if cores}
        if not figures:
            logger.warning("calibration run %s produced no usable measurement; node %s stays "
                           "on the declared sizing", job_key, node_id)
            self._discard.discard(job_key)
            return False
        self._by_node[node_id] = figures
        logger.info("node %s calibrated from %s: %s", node_id, job_key,
                    ", ".join(f"{k}={v:g}" for k, v in sorted(figures.items())))
        return True

    def abandon(self, node_id, job_key) -> None:
        """A probe that will never report. Frees the node for the next job to calibrate it.

        Its results are *kept*: the run happened at the declared sizing, which is what every
        other run on an uncalibrated node also used, so it is as comparable as they are. Only
        a probe whose figures were actually adopted has to be dropped.
        """
        if self._probes.get(node_id) == job_key:
            self._probes.pop(node_id, None)
            self._discard.discard(job_key)

    def should_discard(self, job_key) -> bool:
        """Whether this job's results must be dropped from the campaign."""
        return job_key in self._discard


def calibration_applies(total_jobs: int, node_count: int) -> bool:
    """Whether a campaign is worth calibrating at all.

    No tuned constant, because none is needed: calibration costs one run per node and only
    pays where a node runs a *second* job. With no more jobs than nodes, no node gets one --
    so a pilot would spend its entire result set on measurement. Skipped there, and the
    campaign behaves exactly as it did before any of this existed.
    """
    return node_count > 0 and total_jobs > node_count
