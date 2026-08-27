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

**How the figure is found: one probe per node, which is never a campaign run.**

Before a node takes any of the campaign's work, one *probe* runs there at the declared size.
What it measured becomes that node's figures, and every campaign run on that node then uses
them -- so all of them share one environment, which is the property the whole exercise is
for.

**The probe is not a run that gets removed; it is a run that is never added.** It writes to
``_calibration/`` (see ``RESERVED_CAMPAIGN_DIRS``), which nothing walks looking for runs, so
it cannot enter the results in the first place. Taking one of the campaign's own runs and
deleting it afterwards would be the more dangerous design by some distance -- a mistake there
costs results that cannot be recovered -- and it would also hand back a campaign of 46 runs
where 50 were asked for. This costs the same wall-clock and delivers all 50.

**A node with an outstanding probe takes no campaign work.** Without that, jobs land there at
the declared size while the probe is still running, and those runs are then the odd ones out
on a node whose later runs are calibrated -- reintroducing the very inconsistency the probe
exists to remove. The cost is one run's worth of ramp-up at campaign start, in parallel
across the nodes.

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

#: Never size a container below this, whatever it measured. A last-resort floor, not the
#: defence against a short probe -- that is :data:`MIN_PROBE_SAMPLES` below, because a floor
#: cannot tell "this container genuinely idles" from "this run stopped before it started".
MIN_CPU = 0.25

#: Ticks a probe must have produced before its measurement is believed. The monitor samples
#: about once a second, so this is roughly half a minute of the trial actually running.
#:
#: **This is a correctness gate, not a refinement.** The monitor writes its CSV whether or not
#: the scenario succeeds, so a probe that died ten seconds in still produces a file -- one
#: whose peak is near nothing. Believed, it would floor the whole node to :data:`MIN_CPU` and
#: then *every* campaign run placed there would be starved by an allocation derived from a
#: run that never happened. A measurement failure would have become silently degraded results
#: on one node, which is the failure class this whole area exists to remove.
MIN_PROBE_SAMPLES = 30


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

    #: node_id -> {container_name: {"sustained": cores, "peak": cores}}, headroom applied
    _by_node: dict = field(default_factory=dict)
    #: node_id -> the probe key currently measuring that node
    _probes: dict = field(default_factory=dict)
    enabled: bool = True

    def calibrated(self, node_id) -> "dict | None":
        """That node's per-container cores, or ``None`` while it is still unknown."""
        return self._by_node.get(node_id)

    def claim_probe(self, node_id, probe_key) -> bool:
        """Start measuring *node_id*, unless it is measured or being measured already.

        **At most one outstanding per node**, which is the whole reason this is a claim rather
        than a flag: without it every job of the first wave becomes a probe and the campaign
        pays for its calibration once per job instead of once per node.
        """
        if not self.enabled or node_id is None or node_id in self._by_node:
            return False
        if node_id in self._probes:
            return False
        self._probes[node_id] = probe_key
        return True

    def accepts_work(self, node_id) -> bool:
        """Whether a campaign job may be placed on *node_id* yet.

        ``False`` only while a probe is out. Every campaign run on a node must use that
        node's figures, so work placed before the probe reports would be sized differently
        from everything that follows it -- the inconsistency the probe exists to remove,
        reintroduced by the act of measuring.

        A node needing a probe it has not been given yet DOES accept work, and that is not a
        contradiction: calibration is disabled for such a campaign (a pilot), or the node is
        unlabelled and cannot be sized per node anyway.
        """
        return node_id not in self._probes

    def record(self, node_id, job_key, measured: dict, *, completed: bool = True) -> bool:
        """Take a finished probe's per-container measurement as that node's figures.

        *measured* is ``{container: {"sustained": cores, "peak": cores}}`` -- both, because
        the right statistic depends on what the container is FOR and this module must not
        know that. A system under test is sized on its peak so it never throttles; a
        simulator is sized on what it sustains and allowed to burst past it. Keeping both
        here leaves that choice with the caller that knows the roles.

        Returns whether anything was stored: a probe that produced no measurement leaves the
        node uncalibrated, so the next job there becomes the probe instead. Silence is not a
        measurement of zero.
        """
        if self._probes.get(node_id) != job_key:
            return False
        self._probes.pop(node_id, None)
        if not completed:
            # A probe whose scenario never reached a verdict measured a fragment of a run.
            # The node stays on the declared sizing, which is merely un-optimised, rather
            # than on a figure derived from a run that did not happen.
            logger.warning("calibration probe %s did not complete; node %s stays on the "
                           "declared sizing", job_key, node_id)
            return False
        thin = [name for name, stats in (measured or {}).items()
                if (stats or {}).get("samples", 0) < MIN_PROBE_SAMPLES]
        if thin:
            logger.warning("calibration probe %s produced too few samples for %s "
                           "(< %d ticks); node %s stays on the declared sizing",
                           job_key, ", ".join(sorted(thin)), MIN_PROBE_SAMPLES, node_id)
            return False
        figures = {}
        for name, stats in (measured or {}).items():
            kept = {k: max(MIN_CPU, round(v * CALIBRATION_HEADROOM, 3))
                    for k, v in (stats or {}).items() if v and k != "samples"}
            if kept:
                figures[name] = kept
        if not figures:
            logger.warning("calibration probe %s produced no usable measurement; node %s "
                           "stays on the declared sizing", job_key, node_id)
            return False
        self._by_node[node_id] = figures
        logger.info("node %s calibrated from %s: %s", node_id, job_key,
                    ", ".join(f"{k}={v.get('peak', 0):g}peak/{v.get('sustained', 0):g}sust"
                              for k, v in sorted(figures.items())))
        return True

    def abandon(self, node_id, probe_key) -> None:
        """A probe that will never report -- it died, or the campaign is shutting down.

        Frees the node so it can accept work again. The node stays uncalibrated and its runs
        use the declared sizing, which is the same thing that happens on a cluster where
        calibration is off entirely: a worse allocation, never a wrong result.
        """
        if self._probes.get(node_id) == probe_key:
            self._probes.pop(node_id, None)


#: Turns per-node *sizing* on. **Off by default, and the default is the honest one.**
#:
#: A probe runs before the campaign places any work, which is what lets every run on a node
#: share one environment -- and is also why its figures are not the campaign's. Measured on
#: 2026-08-27, same node, same campaign: the probe read the system under test at 0.82 peak
#: where its real runs peaked at 1.64, and read the simulator's sustained use at 1.13 where
#: the real runs sat at 0.35. Under by 2x on the container that must never be starved, over
#: by 3x on the ones the density was supposed to come from -- so applying it would starve
#: nav2 below its measured floor AND give back the packing gain.
#:
#: The two goals are simply exclusive: "measured before any load" and "measured under the
#: load it will meet" cannot both hold of one probe. Until a probe measures under
#: representative contention, declared sizing everywhere is the correct behaviour, and
#: per-node PLACEMENT -- budgets and pinning, which are verified and fix fragmentation --
#: stands on its own without it.
CALIBRATION_ENV = "ROBOVAST_NODE_CALIBRATION"


def calibration_enabled() -> bool:
    """Whether per-node sizing is switched on. See :data:`CALIBRATION_ENV`."""
    import os  # noqa: PLC0415

    return (os.environ.get(CALIBRATION_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def calibration_applies(total_jobs: int, node_count: int, growable: bool = False) -> bool:
    """Whether a campaign is worth calibrating at all.

    **Never on a cluster that can grow.** There, a job that fits no current node is created
    *unpinned* and the scheduler places it -- possibly on a node that is already calibrated,
    at the declared size, which is precisely the mixed-sizing this exists to prevent and is
    invisible after the fact. An autoscaler's node set is fluid anyway: a figure measured on
    a node that is about to be scaled away is a probe run spent on nothing. Declared sizing
    everywhere is the honest behaviour there.

    No tuned constant, because none is needed: calibration costs one probe run per node and
    only pays where a node then runs several jobs at the better size. With no more jobs than
    nodes, no node runs more than one, so the probe would cost as much as the work it was
    meant to improve. Skipped there, and the campaign behaves exactly as it did before any of
    this existed.
    """
    return (calibration_enabled() and not growable
            and node_count > 0 and total_jobs > node_count)


def container_cpu_profile(rows) -> dict:
    """``{"sustained": cores, "peak": cores}`` from one ``resource_usage_<container>.csv``.

    *rows* are the CSV's dict rows. Summed **per tick before aggregating**, because a row is
    one process name and a container is the whole stack of them: taking the max of the rows
    would report the busiest single process and size the container for a fraction of itself.

    ``sustained`` is the 95th percentile of the per-tick totals and ``peak`` the largest. The
    pair exists because one number cannot serve both roles -- measured on the shipped
    example, a simulator sustains 0.34 cores and peaks at 5.98, so sizing it at either figure
    alone is wrong by about 18x in one direction or the other.

    Returns ``{}`` when there is nothing to read, which the caller must treat as "not
    measured" rather than as zero.
    """
    per_tick = {}
    for row in rows or []:
        try:
            ts = row["timestamp"]
            cpu = float(row["cpu_percent"] or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        per_tick[ts] = per_tick.get(ts, 0.0) + cpu
    if not per_tick:
        return {}
    totals = sorted(v / 100.0 for v in per_tick.values())
    idx = max(0, min(len(totals) - 1, int(round(0.95 * (len(totals) - 1)))))
    # ``samples`` travels with the figures so the caller can refuse a measurement drawn from
    # too little of a run -- see MIN_PROBE_SAMPLES. A short probe is not a small container.
    return {"sustained": totals[idx], "peak": totals[-1], "samples": len(totals)}


def read_probe_measurement(read, prefix: str, containers) -> dict:
    """``{container: profile}`` from a finished probe's own CSVs.

    *read* is ``(key) -> bytes | None``, so this needs no storage client and can be tested
    without one. *containers* maps a container name to the file the monitor wrote for it
    (``main`` for the pod's main container, the role name for each sidecar).

    **Read directly, never through postprocessing.** The file the monitor writes IS the
    measurement -- postprocessing only lifts it into ``data.db``, and does so at the end of a
    campaign or a batch, which is far too late to size the job that comes next. It is also
    why the probe's directory being skipped by postprocessing costs nothing: there was never
    anything to gain from it going through.

    A container whose file is missing or unreadable is simply absent from the result, which
    the caller must read as "not measured" -- and, because a partial pod cannot be sized
    coherently, that is what makes the whole probe unusable rather than most of it.
    """
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415

    out = {}
    for name, filename in (containers or {}).items():
        try:
            raw = read(f"{prefix}{filename}")
        except Exception as exc:  # noqa: BLE001 - an unreadable probe is a missed
            logger.debug("probe file %s unreadable: %s", filename, exc)
            continue
        if not raw:
            continue
        try:
            rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))
        except Exception as exc:  # noqa: BLE001 - see above
            logger.debug("probe file %s unparseable: %s", filename, exc)
            continue
        profile = container_cpu_profile(rows)
        if profile:
            out[name] = profile
    return out


#: Where a probe's output goes, under the campaign root. Reserved (see
#: ``RESERVED_CAMPAIGN_DIRS``), so nothing walks it looking for runs.
PROBE_DIR = "_calibration"


def probe_output_dir(node_id: str) -> str:
    """The campaign-relative directory a probe of *node_id* writes into."""
    return f"{PROBE_DIR}/{node_id}"


def probe_parameter_documents(documents, node_id: str) -> list:
    """A job's parameter documents, redirected so a probe writes outside the run tree.

    **This is the half that is easy to miss, and missing it is the whole hazard.** A job's
    output arrives in two places by two different mechanisms:

    * its *job artifacts* -- the monitor's CSVs, the logs -- go where ``OUTPUT_DIR`` says;
    * its *scenario results* -- rosbags, ``test.xml``, poses -- go where the parameter
      document's ``_output_dir`` says, which is normally ``<config_name>/<run_number>``.

    Overriding only ``OUTPUT_DIR`` therefore leaves a probe writing its results into a REAL
    campaign run directory: colliding with that run, or manufacturing one that looks real.
    Both have to point at :data:`PROBE_DIR`, and this is the second one.

    The rest of the document is left exactly as it was. A probe has to run the same
    configuration a real job would -- including recording its bags, which is not free and is
    therefore part of what has to be measured. A probe that skipped recording would measure a
    lighter workload than the runs it is sizing, and would under-size the node.
    """
    import copy  # noqa: PLC0415

    out = []
    for document in documents or []:
        doc = copy.deepcopy(document)
        for params in doc.values():
            if isinstance(params, dict):
                params["_output_dir"] = probe_output_dir(node_id)
        out.append(doc)
    return out
