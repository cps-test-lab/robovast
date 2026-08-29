# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Per-node container sizing, learned from one discarded run per node.

**Why per node at all.** The same trial costs about 1.6x more CPU on the slowest machine of a
mixed cluster than on the fastest, and wall time does not show it -- a realtime-paced
simulator holds one simulated second per wall second, so every machine finishes at roughly
the same time and the difference lands entirely in CPU consumed. One declared number is
therefore wrong on every node but the one it was measured on: sized for the fast node it
starves the slow one, sized for the slow node it wastes the fast one.

**And it is a validity matter, not only a throughput one.** One declaration applied to unlike
machines can bind on the slow one while leaving the fast one unconstrained, so the system
under test meets its deadlines on some nodes and not others -- ``run_validity_view`` reports
that as ``quota_bound`` per run, per node. Equal *cores* are not equal *compute*, so an equal
declaration produces unequal conditions, which is the thing a uniform number was supposed to
prevent. Sizing each node so the stack meets its deadlines everywhere equalises the behaviour
instead of the accounting.

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

from robovast.common.campaign_data import PROBE_DIR as _PROBE_DIR

logger = logging.getLogger(__name__)

#: Headroom over what a calibration run was measured using. The same figure ``advice.py``
#: applies when it suggests a reservation, and for the same reason: a measured peak is one
#: sample of a distribution, and sizing exactly at it guarantees the next run is clipped.
CALIBRATION_HEADROOM = 1.25

#: Never size a container below this, whatever it measured. A last-resort floor, not the
#: defence against a short probe -- that is :data:`MIN_PROBE_SAMPLES` below, because a floor
#: cannot tell "this container genuinely idles" from "this run stopped before it started".
MIN_CPU = 0.25

#: Fewest ticks a percentile may be read from. **A statistical floor, and only that.**
#:
#: It was 30 -- half a minute at the monitor's ~1 Hz -- and carried a second job it should
#: never have had: catching a probe that died partway through. That made it a duration
#: assertion, so a campaign whose trials run under 30 s could never calibrate; every probe
#: was rejected as thin and every node silently kept what it started on. Once a refused probe
#: FAILS the campaign, that stops being a quiet mis-sizing and becomes a campaign that always
#: fails.
#:
#: **The other job now has an exact answer of its own**: :func:`probe_completed` reads the
#: scenario's ``test.xml``, which exists only once a run reaches a verdict. "Did the probe
#: run to the end" is therefore not this constant's question, and scaling it with the trial
#: would answer a question already answered -- with the trial's *timeout*, which is an outer
#: backstop rather than an expected duration, and is routinely fifty times the real one.
#:
#: What is left is the honest question: how many samples a percentile needs to mean anything.
#: That does not grow with the trial.
MIN_PROBE_SAMPLES = 10


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
    #: node_id -> why that node's probe was refused. Kept as data rather than only logged so
    #: the caller can name the reason in the failure it raises: the refusal is decided here,
    #: but what it MEANS for a campaign is not this store's business.
    _refused: dict = field(default_factory=dict)
    enabled: bool = True

    def calibrated(self, node_id) -> "dict | None":
        """That node's per-container cores, or ``None`` while it is still unknown."""
        return self._by_node.get(node_id)

    def outcome(self) -> dict:
        """``{"calibrated": [node_id, ...], "refused": {node_id: reason}}``."""
        return {"calibrated": sorted(self._by_node), "refused": dict(self._refused)}

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

    def record(self, node_id, job_key, measured: dict, *, completed: bool = True,
               percentiles=None, min_samples: int = None, tick_ratio=None) -> bool:
        """Take a finished probe's per-container measurement as that node's figures.

        *measured* is ``{container: {"sustained": cores, "peak": cores}}`` -- both, because
        the right statistic depends on what the container is FOR and this module must not
        know that. A system under test is sized on its peak so it never throttles; a
        simulator is sized on what it sustains and allowed to burst past it. Keeping both
        here leaves that choice with the caller that knows the roles.

        Returns whether anything was stored: a probe that produced no measurement leaves the
        node uncalibrated, so the next job there becomes the probe instead. Silence is not a
        measurement of zero.

        *percentiles* is ``{container: percentile}`` -- what each container's figure was read
        at, resolved by the caller from the campaign and the role rules. Passed in rather
        than decided here for the reason this store keeps generally: it must not know what a
        container is *for*. It is needed because the throttling a probe may survive follows
        the percentile its figure comes from (see :func:`probe_refuse_ratio`). A container
        absent from the map is judged strictly, since an unnamed one might be the one read at
        its maximum, and accepting a distorted maximum writes a wrong figure in silently
        where refusing merely leaves the node unmeasured.

        *min_samples* is the floor a measurement must clear, derived by the caller from the
        trial's own length; :data:`MIN_PROBE_SAMPLES` is the absolute lower bound it may not
        go below.

        Five things are refused, and each leaves the node on its declared sizing rather than
        on a figure that would be wrong: a probe whose scenario reached no verdict, one drawn
        from too few samples, one that produced nothing usable, one **throttled past what its
        own statistic can absorb** (see :func:`probe_refuse_ratio`), and one that was
        **OOM-killed**.

        The last two are not the same shape. A CPU ceiling that binds slows the container
        down, so what is refused is a *ratio* above a threshold with an allowance for
        bring-up. A memory ceiling that binds KILLS it, so one kill is enough: the file
        records a fragment of a run that died rather than a measurement of one that
        finished.

        A node where either counter cannot be read is calibrated without that check, because
        absent is not zero and refusing on absence would leave such a cluster permanently
        uncalibrated.
        """
        if self._probes.get(node_id) != job_key:
            return False
        self._probes.pop(node_id, None)
        if not completed:
            # A probe whose scenario never reached a verdict measured a fragment of a run.
            # The node keeps what it is already running on -- the declared figures, or the
            # bootstrap under `sizing: calibrated` -- which is merely un-optimised, rather
            # than a figure derived from a run that did not happen.
            logger.warning("calibration probe %s did not complete; node %s keeps its current "
                           "sizing (declared, or the bootstrap)", job_key, node_id)
            self._refused[node_id] = "its probe reached no verdict"
            return False
        floor = max(MIN_PROBE_SAMPLES, int(min_samples or 0))
        thin = [name for name, stats in (measured or {}).items()
                if (stats or {}).get("samples", 0) < floor]
        if thin:
            logger.warning("calibration probe %s produced too few samples for %s "
                           "(< %d ticks); node %s keeps its current sizing",
                           job_key, ", ".join(sorted(thin)), floor, node_id)
            self._refused[node_id] = f"its probe produced fewer than {floor} samples"
            return False
        # A probe that hit its own ceiling measured the ceiling. Refused rather than stored,
        # because the figure would be a limit dressed as a demand and every later run on this
        # node would inherit it -- and nothing downstream can tell the two apart afterwards.
        # A memory ceiling that binds does not throttle, it KILLS -- so unlike the CPU
        # case there is no ratio to weigh and no bring-up allowance to make. One kill
        # means the container did not run to the end, and whatever the file records is a
        # fragment of a run that died rather than a measurement of one that finished.
        killed = sorted(name for name, stats in (measured or {}).items()
                        if (stats or {}).get("oom_kills", 0) > 0)
        if killed:
            logger.warning(
                "calibration probe %s was OOM-killed (%s); node %s keeps its current "
                "sizing. The memory it was given is too small for this campaign -- see "
                "ROBOVAST_BOOTSTRAP_MEMORY, or declare it with execution.sizing: fixed",
                job_key, ", ".join(killed), node_id)
            self._refused[node_id] = (
                "its probe was OOM-killed (" + ", ".join(killed) + ")")
            return False
        capped = {name: stats["throttled_ratio"]
                  for name, stats in (measured or {}).items()
                  if (stats or {}).get("throttled_ratio", 0)
                  > probe_refuse_ratio((percentiles or {}).get(name, 100.0))}
        if capped:
            logger.warning(
                "calibration probe %s was throttled past what its own statistic can absorb "
                "(%s); node %s keeps its current sizing, which is what the probe was "
                "measured against",
                job_key,
                ", ".join(f"{k}={v:.1%}" for k, v in sorted(capped.items())),
                node_id)
            self._refused[node_id] = (
                "its probe was throttled past what its statistic absorbs ("
                + ", ".join(f"{k}={v:.1%}" for k, v in sorted(capped.items())) + ")")
            return False
        # **Stored raw.** Headroom and the floor are applied where the allocation is built,
        # because both are per-container settings now and this store deliberately does not
        # know what a container is for. It records what was measured; the caller decides what
        # to do with it.
        # The scenario runner's own report on itself, and the only guard that container has.
        # It is sized on a percentile with a ceiling it may burst into, so it can be starved
        # for a whole run without ever hitting its quota -- invisible to the throttle counter
        # that catches this for everything else. `None` is "not measured" and never refuses.
        if tick_ratio is not None and tick_ratio > PROBE_TICK_REFUSE_RATIO:
            logger.warning(
                "calibration probe %s ticked at %.1fx its configured period; node %s keeps "
                "its current sizing. The scenario runner could not hold its rate while being "
                "measured, so the figures would be a starved container's",
                job_key, tick_ratio, node_id)
            self._refused[node_id] = (
                f"its scenario runner ticked at {tick_ratio:.1f}x its configured period")
            return False
        figures = {}
        for name, stats in (measured or {}).items():
            kept = {k: v for k, v in (stats or {}).items()
                    if v and k not in ("samples", "throttled_ratio", "oom_kills")}
            if kept:
                figures[name] = kept
        if not figures:
            logger.warning("calibration probe %s produced no usable measurement; node %s "
                           "keeps its current sizing", job_key, node_id)
            self._refused[node_id] = "its probe produced no usable measurement"
            return False
        # A node that succeeds is not a refused node, whatever an earlier probe said: the
        # ledger reports the END state.
        self._refused.pop(node_id, None)
        self._by_node[node_id] = figures
        # INFO on a `robovast.*` logger, so it reaches the campaign log as well as the
        # service's: what a node's jobs were sized to is part of what the campaign did, and
        # a reader asking why two nodes behaved differently should find it there.
        logger.info(
            "node %s calibrated from %s -- measured, before headroom: %s",
            node_id, job_key,
            ", ".join(f"{k}={v.get('cores', 0):g}cores"
                      + (f"/{v['memory_peak'] / (1024 ** 3):.2f}GiB"
                         if v.get("memory_peak") else "")
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


def calibration_applies(total_jobs: int, node_count: int, growable: bool = False) -> bool:
    """Whether a campaign that ASKED to be calibrated can be.

    Whether it asked is ``execution.sizing``, decided per campaign in its ``.vast`` and not
    here -- this answers only whether the cluster and the campaign's shape make a probe
    worth running.

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
    return not growable and 0 < node_count < total_jobs


#: What a container asks for before anything has been measured for it, under
#: ``execution.sizing: calibrated``. From the service's environment, not from a ``.vast``.
#:
#: **A property of the cluster, not of the campaign** -- the same argument that takes the
#: figure out of the ``.vast`` in the first place. A core count is a fact about the machine,
#: so the person who knows it is the one who set the cluster up.
#:
#: **Per ROLE, because the three want very different amounts.** A flat figure over-reserves
#: the scenario and under-reserves the system under test, and the probe is the run that most
#: needs not to bind: one throttled against its own ceiling measures the bootstrap rather
#: than the workload, which :data:`PROBE_THROTTLE_REFUSE_RATIO` then refuses.
#:
#: An ad-hoc container -- any name outside ``CONTAINER_ROLES`` -- takes
#: :data:`DEFAULT_BOOTSTRAP_OTHER`. Nothing is known about it, and small is the conservative
#: direction for an unknown: it is one probe away from a measured figure, while a generous
#: default for every unnamed container is what makes a probe unplaceable.
#:
#: This is also the floor on what calibration COSTS, since every probe is a pod sized from
#: it -- raising it makes probes harder to place on a small or busy cluster.
#:
#: **CPU and memory rank the roles differently, and that is not a mistake.** The system
#: under test wants cores and little memory; the simulator is the opposite -- it compiles a
#: world once and then sustains very little CPU, so it is the memory outlier. Sizing both
#: from one ranking would starve whichever resource the other role dominates.
#:
#: A memory bootstrap that is too small does not throttle, it OOM-kills: the probe dies, the
#: node stays uncalibrated, and the campaign carries on at the bootstrap -- a sizing fault
#: wearing the stack's clothes. `memory.events`' `oom_kill` counter is sampled and could be
#: read here, which is the memory half of what PROBE_THROTTLE_REFUSE_RATIO does for CPU.
#: **Each figure is also what the PROBE runs at, so none of them may be tight.** A probe
#: capped below what its container wants throttles against that cap; the guard then refuses
#: it for having measured the cap rather than demand, no node is calibrated, and the campaign
#: runs at this bootstrap for its whole life -- the outcome calibration exists to avoid,
#: reached by tightening the one figure that must not be tight.
#:
#: So each is sized on its container's PEAK, not its average, and the peak that matters is
#: bring-up: a ROS stack costs several times its steady-state CPU while its lifecycle nodes
#: come up, and that is also where a cap does the most damage, since a transition that misses
#: its deadline fails the trial before it starts. A figure near the average therefore looks
#: ample on a graph and still deadlocks calibration.
#:
#: To re-derive them, read a probe's own ``system_usage_<container>.csv`` and take the max,
#: not the campaign log -- what that prints has ``advice.CPU_HEADROOM`` already applied and
#: overstates the measurement by that factor.
DEFAULT_BOOTSTRAP_CPU = {"sut": 8, "simulation": 3, "scenario": 2}
DEFAULT_BOOTSTRAP_MEMORY = {"sut": "2Gi", "simulation": "4Gi", "scenario": "1Gi"}
DEFAULT_BOOTSTRAP_OTHER = (1, "1Gi")

#: JSON, ``{role: value}``, overriding the defaults per role. A role absent from an override
#: keeps its default rather than disappearing, so raising one role does not silently drop the
#: others. Unparseable raises rather than defaulting, for the reason the headroom does: a
#: typo that silently became something else would mis-size every job of every calibrated
#: campaign, and the symptom appears nowhere near the cause.
BOOTSTRAP_CPU_ENV = "ROBOVAST_BOOTSTRAP_CPU"
BOOTSTRAP_MEMORY_ENV = "ROBOVAST_BOOTSTRAP_MEMORY"


#: JSON, ``{role: {field: value}}``, the same block a ``.vast`` container may carry under
#: ``calibration``. Keyed by role like the bootstrap above rather than inventing a second
#: shape, and a whole block rather than one variable per field, so an option added to
#: :class:`~robovast.common.config.CalibrationConfig` later is settable here with no new
#: variable and no change to this module.
CALIBRATION_ENV = "ROBOVAST_CALIBRATION"

#: The percentile each role's figure is taken at when nothing states one, and whether its
#: limit equals its request. **The rule is which guard would report that a squeeze cost
#: something**, not whether the container is under test:
#:
#: * the system under test is read at its max and never throttles, because a run clipped
#:   mid-plan is a failure that looks like the stack's rather than the allocation's;
#: * the simulator is read at a percentile and keeps its ceiling -- its peak-to-mean is about
#:   18, so reserving the max would cost more than not calibrating, and the realtime factor
#:   reports if the squeeze cost anything;
#: * the scenario runner is read the same way, but nothing grades how well it ran, so its
#:   *ceiling* is what must not be tight. On a probe its own tick ratio fills that gap.
DEFAULT_SIZE_ON = {"sut": 100.0}
DEFAULT_SIZE_ON_OTHER = 95.0
DEFAULT_LIMIT = {"sut": "request"}
DEFAULT_LIMIT_OTHER = "declared"


def _mem_headroom() -> float:
    """``advice.MEM_HEADROOM``, imported where it is used rather than copied.

    ``advice`` is already the authority for memory sizing and reads the very counter this
    module now reads; a second constant here would be one to keep in step by hand.
    """
    from robovast.results_processing.advice import MEM_HEADROOM  # noqa: PLC0415

    return MEM_HEADROOM


def calibration_defaults(role: str) -> dict:
    """The ``.env`` entry for *role* over the built-in rule, as a plain dict.

    The ``.vast``'s own block is applied over this by the caller, which is why this returns
    data rather than a model: three layers merge per field, and only the outermost is a
    validated model.
    """
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415

    out = {
        "size_on": DEFAULT_SIZE_ON.get(role, DEFAULT_SIZE_ON_OTHER),
        "limit": DEFAULT_LIMIT.get(role, DEFAULT_LIMIT_OTHER),
        "headroom": {"cpu": CALIBRATION_HEADROOM, "memory": _mem_headroom()},
    }
    raw = (os.environ.get(CALIBRATION_ENV) or "").strip()
    if not raw:
        return out
    try:
        override = json.loads(raw)
        if not isinstance(override, dict):
            raise ValueError("not an object")
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"{CALIBRATION_ENV}={raw!r}: expected JSON like "
            '\'{"sut": {"size_on": 100, "limit": "request"}}\'') from exc
    entry = override.get(role) or {}
    if not isinstance(entry, dict):
        raise ValueError(
            f"{CALIBRATION_ENV}: the entry for {role!r} must be an object, got {entry!r}")
    for key, value in entry.items():
        if key == "headroom" and isinstance(value, dict):
            out["headroom"] = {**out["headroom"],
                               **{k: v for k, v in value.items() if v is not None}}
        elif value is not None:
            out[key] = value
    return out


def _bootstrap_override(env_name: str, defaults: dict) -> dict:
    """*defaults* with the JSON in *env_name* applied over it, or raise."""
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415

    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        return dict(defaults)
    try:
        override = json.loads(raw)
        if not isinstance(override, dict):
            raise ValueError("not an object")
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"{env_name}={raw!r}: expected JSON like "
            '\'{"sut": 8, "simulation": 3, "scenario": 2}\'') from exc
    merged = dict(defaults)
    merged.update({str(k): v for k, v in override.items()})
    return merged


#: Non-empty once the effective bootstrap has been logged. Per process, not per call: it is
#: read for every container of every job and the answer cannot differ between them. A set
#: rather than a flag so the module never rebinds it -- mutating a container needs no
#: ``global``, and a test can clear it without reaching for one either.
_BOOTSTRAP_LOGGED = set()


def _log_bootstrap_once(cores: dict, mems: dict, overridden: bool) -> None:
    """State the figures in the service log, and whether they were configured or defaulted.

    An operator reading the log should not have to infer which of the two they are looking
    at: "these are the defaults" and "these are what I set" are different facts, and only
    one of them means the `.env` was picked up.
    """
    if _BOOTSTRAP_LOGGED:
        return
    _BOOTSTRAP_LOGGED.add(True)
    where = "from the environment" if overridden else "defaults; set them in .env to change"
    logger.info("bootstrap sizing (%s): %s", where,
                ", ".join(f"{r}={cores.get(r)}cpu/{mems.get(r)}"
                          for r in sorted(set(cores) | set(mems))))


def bootstrap_sizing(role: "str | None" = None) -> "tuple[float, int]":
    """``(cores, bytes)`` *role* asks for before its node has been measured.

    Per CONTAINER, not per pod: a three-container pod reserves the sum of the three.
    """
    from .kube_client import parse_resource  # noqa: PLC0415

    import os  # noqa: PLC0415

    cores = _bootstrap_override(BOOTSTRAP_CPU_ENV, DEFAULT_BOOTSTRAP_CPU)
    mems = _bootstrap_override(BOOTSTRAP_MEMORY_ENV, DEFAULT_BOOTSTRAP_MEMORY)
    other_cpu, other_mem = DEFAULT_BOOTSTRAP_OTHER
    _log_bootstrap_once(cores, mems, bool((os.environ.get(BOOTSTRAP_CPU_ENV) or "").strip()
                                          or (os.environ.get(BOOTSTRAP_MEMORY_ENV) or "").strip()))

    cpu = parse_resource(cores.get(role, other_cpu))
    mem = int(parse_resource(mems.get(role, other_mem)))
    if not cpu or not mem:
        raise ValueError(
            f"bootstrap for role {role!r} resolves to cpu={cores.get(role, other_cpu)!r} "
            f"memory={mems.get(role, other_mem)!r}, which are not resource quantities.")
    return cpu, mem


#: Fraction of CFS enforcement periods in which the probe's own container was throttled,
#: above which its measurement is refused.
#:
#: **A throttled probe measured its ceiling, not its demand.** The probe runs at the declared
#: sizing, so if that ceiling binds, the peak it reports is the cap -- and sizing the node
#: from it would write the cap in as though it were what the container needed. Every later
#: run on that node then gets a figure derived from a limit rather than from a workload.
#:
#: Zero is the wrong threshold: a container is briefly throttled during bring-up on any
#: machine, and refusing every probe for that would leave a cluster permanently uncalibrated.
#: This matches ``advice.THROTTLE_WARN_RATIO``, which was calibrated against a sweep in which
#: the stack's own miss count was counted at each level, and carries the same caveat -- it is
#: derived from a 20 Hz control loop, so a slower one tolerates proportionally more.
PROBE_THROTTLE_REFUSE_RATIO = 0.005

def probe_refuse_ratio(percentile: float) -> float:
    """How much throttling invalidates a probe's measurement of ONE container.

    **A container's tolerance is the complement of the percentile its figure comes from.**
    Clipping removes the top of the distribution, so what it destroys depends on where the
    figure is read: one taken at the maximum is spoiled by the first clipped tick, while one
    taken at the 95th percentile is untouched as long as the clipped ticks stay inside the
    top 5% it already discards.

    So the tolerance is *derived* from the percentile rather than being a second number
    beside it -- 5% at 95, 1% at 99 -- and a container read at the maximum falls back to the
    strict floor. That floor is not zero for the reason
    :data:`PROBE_THROTTLE_REFUSE_RATIO` gives: bring-up briefly throttles a container on any
    machine, and refusing every probe for that would leave a cluster permanently
    uncalibrated.

    A single strict ratio for all of them refuses probes whose figure is perfectly good and
    leaves those nodes unmeasured -- a distortion that by construction cannot reach the
    number being read.
    """
    return max(PROBE_THROTTLE_REFUSE_RATIO, 1.0 - (percentile / 100.0))


#: The two file names the resource monitor writes per container, as a naming contract rather
#: than an import: ``monitor_resources`` runs inside the experiment container and this package
#: must not import it (nor it this one). Restated here, and pinned by a test, because the
#: alternative is reaching across a boundary that exists on purpose.
_RESOURCE_PREFIX = "resource_usage_"
_SYSTEM_PREFIX = "system_usage_"


def percentile_of(sorted_or_not, percentile: float) -> float:
    """The value at *percentile* of *sorted_or_not*, where ``100`` is the maximum.

    One implementation for both readers and for every role, because the percentile is now a
    per-container setting rather than a constant: the 100th percentile of a finite sample is
    its largest value, so "the peak" is not a separate code path.
    """
    values = sorted(sorted_or_not)
    if not values:
        return 0.0
    idx = max(0, min(len(values) - 1,
                     int(round((percentile / 100.0) * (len(values) - 1)))))
    return values[idx]


def container_cpu_profile_from_billing(rows, percentile: float = 95.0) -> dict:
    """``{"cores": ..., "memory_peak": ...}`` from one ``system_usage_<container>.csv``.

    *percentile* is the container's own, resolved before this is called, so the choice of
    statistic lives with the role rules rather than here -- this reads what it is told to.

    **Preferred over :func:`container_cpu_profile` wherever the file exists**, because it is
    the kernel's own billing for the cgroup rather than a sum over what psutil could see.
    ``cpu_usage_usec`` is a monotonic counter of CPU time charged to the container, so cores
    over a tick is simply the delta divided by the elapsed wall time -- and it is immune to
    the artifact that forces the other reader to discard samples: psutil reports a
    newly-seen process's average since *it* started rather than since the last sample, and a
    ROS stack spawning dozens of processes at once therefore reports impossible totals during
    bring-up. There is nothing to clamp here, so no ceiling has to be known to read it.

    Ticks where the counter did not advance, or went backwards, are dropped rather than read
    as idle: a counter that resets means the cgroup was replaced, and a zero delta across a
    restart is not a measurement of nothing.

    Returns ``{}`` when the file carries no usable counter -- which the caller must treat as
    "not measured", exactly as it treats a missing file, and never as zero.
    """
    samples = []
    periods, throttled, oom_kills, memory = [], [], [], []
    for row in rows or []:
        try:
            ts = float(row["timestamp"])
            usec = float(row["cpu_usage_usec"])
        except (KeyError, TypeError, ValueError):
            continue
        samples.append((ts, usec))
        try:
            oom_kills.append(float(row["memory_events_oom_kill"]))
        except (KeyError, TypeError, ValueError):
            pass
        # Same file, same tick: whether the kernel stopped this container while it was
        # being measured. Monotonic counters, so the span is last minus first.
        try:
            periods.append(float(row["nr_periods"]))
            throttled.append(float(row["nr_throttled"]))
        except (KeyError, TypeError, ValueError):
            pass
        # Same file, same tick. `memory_peak` is the kernel's own high-water mark for the
        # cgroup, which is what a memory limit has to clear -- not `memory_current`, a
        # sample that misses whatever happened between two ticks.
        try:
            memory.append(float(row["memory_peak"]))
        except (KeyError, TypeError, ValueError):
            pass
    if len(samples) < 2:
        return {}
    samples.sort()
    totals = []
    for (t0, u0), (t1, u1) in zip(samples, samples[1:]):
        dt, du = t1 - t0, u1 - u0
        if dt <= 0 or du < 0:
            continue          # a counter reset, or two rows at one instant
        totals.append((du / 1e6) / dt)
    if not totals:
        return {}
    out = {"cores": percentile_of(totals, percentile), "samples": len(totals)}
    if memory:
        # The MAX, for every role alike: a CPU limit that binds slows a container, a memory
        # limit that binds kills it, so no role may be sized on a percentile here.
        out["memory_peak"] = max(memory)
    span = (periods[-1] - periods[0]) if len(periods) >= 2 else 0
    if span > 0:
        # Absent when the node cannot report it, and absent is NOT zero -- see `record`.
        out["throttled_ratio"] = max(0.0, (throttled[-1] - throttled[0]) / span)
    if len(oom_kills) >= 2:
        out["oom_kills"] = max(0, int(oom_kills[-1] - oom_kills[0]))
    return out


def container_cpu_profile(rows, limit_cores=None, percentile: float = 95.0) -> dict:
    """``{"cores": ...}`` from one ``resource_usage_<container>.csv``.

    The psutil-derived fallback, used only where the kernel's own billing is unavailable. It
    reports no memory figure: this file is per PROCESS, so its memory column double-counts
    pages shared between a process and its forks, and a memory limit sized from it would be
    wrong in the direction that kills.

    *rows* are the CSV's dict rows. Summed **per tick before aggregating**, because a row is
    one process name and a container is the whole stack of them: taking the max of the rows
    would report the busiest single process and size the container for a fraction of itself.

    *limit_cores* is the container's declared ceiling. Samples above it are discarded as
    impossible -- see below; pass it whenever it is known, because the peak is unusable
    without it.

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
    if limit_cores:
        # **A container cannot exceed its own quota, so a sample that says it did is
        # measurement error -- not a peak.** CFS enforces the quota per ~100ms period, and
        # these are one-second samples, so the average over one cannot exceed the limit.
        #
        # They are there, and they are large. The monitor's CSV covers the container's whole
        # life including bring-up, where psutil reports a newly-seen process's average since
        # it started rather than since the last sample -- and a ROS stack spawns dozens of
        # processes at once. Measured on a 3-core container: 10.4 "cores" outside the trial
        # window against 2.82 inside it. Every other consumer of this data filters on
        # ``in_window``, which postprocessing adds later and the raw file does not carry, so
        # calibration is the one reader that meets the artifact -- and it takes the MAX,
        # which is the worst possible statistic to hand it. Sizing a node from that reserved
        # 14.4 cores for a 3-core container, and 35 on another.
        totals = [t for t in totals if t <= limit_cores]
        if not totals:
            return {}
    # ``samples`` travels with the figures so the caller can refuse a measurement drawn from
    # too little of a run -- see MIN_PROBE_SAMPLES. A short probe is not a small container.
    return {"cores": percentile_of(totals, percentile), "samples": len(totals)}


def read_probe_measurement(read, prefix: str, containers, limits=None,
                           percentiles=None) -> dict:
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
        limit = (limits or {}).get(name)
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
        pct = (percentiles or {}).get(name, 95.0)
        profile = container_cpu_profile(rows, limit_cores=limit, percentile=pct)
        # The kernel's own billing where the node could answer, and the per-process sum only
        # where it could not: `system_usage_` needs no ceiling to be read and no impossible
        # sample discarded, so where both exist the counter wins. Falling back rather than
        # requiring it keeps a cgroup v1 host, or a runtime that exposes no cpu.stat,
        # calibratable instead of silently uncalibrated.
        billing = _billing_profile(read, prefix, filename, percentile=pct)
        if billing:
            profile = billing
        if profile:
            out[name] = profile
    return out


def _billing_profile(read, prefix: str, filename: str, percentile: float = 95.0) -> dict:
    """The ``system_usage_`` sibling of *filename*, read as a profile, or ``{}``.

    Best-effort by construction: every failure here means "fall back to the per-process
    file", never "this probe failed". The sibling is absent on a host whose cgroup exposes
    no CPU accounting, and on any run predating it.
    """
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415

    if not filename.startswith(_RESOURCE_PREFIX):
        return {}
    sibling = _SYSTEM_PREFIX + filename[len(_RESOURCE_PREFIX):]
    try:
        raw = read(f"{prefix}{sibling}")
        if not raw:
            return {}
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))
    except Exception as exc:  # noqa: BLE001 - the per-process file still answers
        logger.debug("probe billing file %s unreadable: %s", sibling, exc)
        return {}
    return container_cpu_profile_from_billing(rows, percentile=percentile)


#: Where a probe's output goes, under the campaign root. Reserved (see
#: ``RESERVED_CAMPAIGN_DIRS``), so nothing walks it looking for runs.
#: Re-exported: the name lives in ``campaign_data`` because both postprocessing
#: lanes must exclude it and neither may import this package.
PROBE_DIR = _PROBE_DIR


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


#: What a run writes when its scenario reaches a verdict, whatever that verdict is.
PROBE_VERDICT_FILE = "test.xml"


#: The file ``scenario-execution --tick-log`` writes, one row per tick of the behaviour tree.
PROBE_TICK_FILE = "tick_timing.csv"

#: How far below its configured rate the scenario runner may tick before its measurement is
#: refused. The ratio is ``interval_s / period_s`` -- achieved against intended -- so 1.0 is
#: perfect and larger is slower.
#:
#: **This is the scenario container's only guard, and it exists only on probes.** Every other
#: role has something that reports a squeeze cost it: the system under test has its own health
#: check, the simulator has the realtime factor. The scenario runner has neither, and it is
#: sized on a percentile with a ceiling it can burst into -- so it can be slow throughout
#: without ever being quota-bound, and a measurement taken while it was starved would size
#: every later run on that node.
#:
#: Generous on purpose: a behaviour tree misses its period for reasons that are not
#: starvation -- a slow action, a blocking call -- and this must refuse a probe that could not
#: get CPU, not one whose scenario had work to do.
PROBE_TICK_REFUSE_RATIO = 2.0


def read_probe_tick_ratio(read, prefix: str):
    """The probe's median achieved-against-intended tick ratio, or ``None``.

    ``None`` where the file is absent or carries no usable row -- not a ratio of 1.0. A
    scenario runner that never wrote one was not measured, and "not measured" must never
    read as "held its rate", which is the same rule the resource counters follow.

    The MEDIAN rather than the worst tick: a behaviour tree stalls for a moment on any
    machine, and one slow tick is not a starved container. What starvation looks like is the
    whole distribution shifting.
    """
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415

    try:
        raw = read(f"{prefix}{PROBE_TICK_FILE}")
        if not raw:
            return None
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))
    except Exception as exc:  # noqa: BLE001 - absent is not a verdict
        logger.debug("probe tick log at %s unreadable: %s", prefix, exc)
        return None
    ratios = []
    for row in rows:
        try:
            interval, period = float(row["interval_s"]), float(row["period_s"])
        except (KeyError, TypeError, ValueError):
            continue
        if period > 0 and interval >= 0:
            ratios.append(interval / period)
    if not ratios:
        return None
    return percentile_of(ratios, 50.0)


def probe_completed(read, prefix: str) -> bool:
    """Did the probe's scenario actually reach a verdict?

    **The gate this exists for was wired to a tautology.** ``record`` took ``completed`` and
    was handed ``bool(measured)`` -- "completed if we measured something" -- which is true of
    every probe that produced a CSV at all, including one whose scenario died ten seconds in.
    The monitor writes that CSV regardless of the outcome, so the check that was supposed to
    catch a fragment of a run caught nothing.

    ``test.xml`` is the honest signal: a run writes it when its scenario reaches a verdict,
    and only then. Pass or fail is not the question -- a run that failed after doing its work
    still measured the resources that work needed -- but a run that never got there did not.
    """
    try:
        return bool(read(f"{prefix}{PROBE_VERDICT_FILE}"))
    except Exception as exc:  # noqa: BLE001 - unreadable is not completed
        logger.debug("probe verdict at %s unreadable: %s", prefix, exc)
        return False
