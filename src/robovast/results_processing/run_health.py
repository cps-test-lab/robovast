# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Per-run health, graded by the stack that knows what healthy means.

A run has exactly one machine-readable outcome today: the scenario's pass/fail. That is too
coarse for the questions this substrate exists to answer. Finding a stack's resource floor
needs a *knee in a curve*, not a boolean. Deciding whether observed throttling actually cost
anything needs the stack's own opinion, because
:func:`~robovast.results_processing.advice.throttle_advice` can only say a resource
explanation is *available*. And grading a reconstruction against a published result is the
whole research question, which "did it pass" cannot express.

So: ``run_health``, one row per (run, check), carrying a **measure** as well as a verdict.

**RoboVAST learns exactly one word.** ``level`` -- ``ok`` / ``warn`` / ``error`` -- and
nothing else. ``check`` and ``detail`` are passed through untouched, exactly as
:class:`~robovast.client.status.HealthFinding` already does for the live simulator findings
this is the post-hoc sibling of. The moment this module knows what a recovery behaviour is,
the contract is dead: every stack would need its thresholds here, and a MoveIt 2 campaign
would be graded by nav2's idea of healthy.

**Three rules, and they are what keep it honest:**

1. **Health never decides pass/fail.** The scenario's verdict is the verdict; this grades it.
   Without that it becomes a second, differently-calibrated failure oracle, and two oracles
   eventually disagree about the same run -- the hazard the nav_search example's own scenario
   warns about in its own words. Nothing here is wired to a run's status, and nothing should
   be.
2. **``ok`` is a row; absence is not a pass.** A run with no row for a check was *not
   checked* -- because no plugin was installed, because the check did not apply to that
   stack, or because the tables it reads were not produced. A missing measurement and a clean
   one must never look alike, which is the same rule ``system_usage`` follows and the same
   reason.
3. **Thresholds belong to the plugin or to the analysis, never here.** A plugin decides what
   ``warn`` means for its stack; a reader who disagrees has ``value`` and can re-threshold it
   without re-running anything. That is why ``value`` exists at all: a finding says *bad*, a
   measure says *how bad*, and a floor is found in the knee of a curve.

**What a check reads.** The run's already-derived tables in ``data.db`` -- for nav2 that
would be ``nav2_behaviors``, ``nav2_behavior_tree`` and the control-loop warnings in
``run_log``; for MoveIt 2, planning time and solve failures. None of that is known here.

**Scoping to the trial, without a phase system.** ``run_log`` carries ``sim_time`` (NULL for
every row written before the clock existed) and ``in_window`` (the trial window). So "after
the clock, during the trial" is ``WHERE in_window = 1 AND sim_time IS NOT NULL`` -- the same
two columns ``resource_usage`` and ``system_usage`` already use, rather than something a
plugin author invents. Finer phases genuinely are missing: only the scenario knows its own
phase structure, and the intended split is that the scenario *declares* boundaries as
markers while the plugin *counts* between them. Deliberately not built yet: the check that
motivated this needed none, since every control-loop miss it counts is ``in_window = 1``.
"""

import inspect
import logging
from dataclasses import dataclass
from importlib.metadata import entry_points

logger = logging.getLogger(__name__)

#: Entry-point group a stack ships its check under, mirroring
#: ``robovast.postprocessing_commands``. A campaign may also name a local ``./path.py:Class``
#: so a system under test can ship one without packaging anything.
HEALTH_GROUP = "robovast.health_checks"

#: The only three words this module interprets. Anything else is a plugin bug: the value
#: cannot be rendered, sorted or filtered on, and guessing would put a stack's private
#: vocabulary into a column readers compare across stacks.
LEVELS = ("ok", "warn", "error")

#: Who noticed. ``stack`` is this post-hoc path; ``simulator`` is reserved for folding in the
#: live :class:`~robovast.client.status.HealthFinding` reports, so one table answers "what was
#: wrong with this run" whoever saw it first.
SOURCE_STACK = "stack"
SOURCE_SIMULATOR = "simulator"

TABLE = "run_health"


@dataclass(frozen=True)
class HealthRow:
    """One check's opinion about one run.

    *value* and *unit* are the addition to the live contract, and the point of it: a floor is
    found in a curve, so a check that can quantify itself should. ``None`` is honest for a
    check that genuinely is a boolean -- it is not a placeholder for "forgot to measure".
    """

    config_name: str
    run_id: int
    check: str
    level: str
    detail: str = ""
    value: "float | None" = None
    unit: "str | None" = None


def load_health_checks(declared=None, config_dir=None) -> dict:
    """Every health check that should run, by name.

    Entry-point plugins are discovered and run **without being declared**, unlike
    postprocessing commands. A postprocessing command produces data and costs time, so asking
    for it is right; a health check reads tables that already exist and contributes nothing
    when it does not apply to the campaign's stack. Requiring a ``.vast`` edit would mean the
    campaigns most in need of grading -- the ones nobody thought about -- are the ones without
    it, and rule 2 makes a silent absence indistinguishable from a clean bill.

    *declared* adds local ``./path.py:Class`` refs from the campaign's config, which is how a
    system under test ships a check for itself without packaging one.
    """
    from robovast.common.plugin_ref import is_file_ref, load_ref  # noqa: PLC0415

    checks = {}
    try:
        for ep in entry_points(group=HEALTH_GROUP):
            try:
                obj = ep.load()
                checks[ep.name] = obj() if inspect.isclass(obj) else obj
            except Exception as exc:  # noqa: BLE001 - one bad plugin must not stop the rest
                logger.warning("health check %r could not be loaded: %s", ep.name, exc)
    except Exception:  # noqa: BLE001 - no entry points at all is not an error
        pass

    for ref in declared or []:
        name = ref if isinstance(ref, str) else next(iter(ref))
        if not is_file_ref(name):
            if name not in checks:
                logger.warning("health check %r is not installed and is not a file "
                               "reference; skipping", name)
            continue
        try:
            obj = load_ref(name, config_dir)
            checks[name] = obj() if inspect.isclass(obj) else obj
        except Exception as exc:  # noqa: BLE001 - see above
            logger.warning("health check %r could not be loaded: %s", name, exc)
    return checks


def _rows_from(check_name, result):
    """Validate one check's return into ``HealthRow``s, dropping what cannot be interpreted.

    Dropped rather than raised: a plugin bug must not cost a campaign its postprocessing,
    and the rest of the campaign's health is still worth having. Logged loudly, because a
    silently dropped row is a run that reads as *not checked* -- which is rule 2's exact
    failure mode and would look identical to a stack that had nothing to say.
    """
    for item in result or []:
        row = item if isinstance(item, HealthRow) else None
        if row is None:
            try:
                row = HealthRow(**item)
            except Exception as exc:  # noqa: BLE001
                logger.warning("health check %r returned an unusable row (%s): %r",
                               check_name, exc, item)
                continue
        if row.level not in LEVELS:
            logger.warning("health check %r returned level %r, not one of %s; dropping the "
                           "row rather than guessing what it meant",
                           check_name, row.level, ", ".join(LEVELS))
            continue
        yield row


def build_run_health_table(conn, checks=None, source: str = SOURCE_STACK) -> int:
    """Create ``run_health`` and fill it from *checks*. Returns the row count.

    **The table is created even when nothing fills it.** An absent table and an empty one say
    different things -- "this campaign predates health checks" versus "they ran and found
    nothing to say" -- and only the second is evidence. Rule 2 is about runs; this is the same
    rule one level up.
    """
    conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
    conn.execute(
        f"CREATE TABLE {TABLE} (config_name TEXT, run_id INTEGER, check_name TEXT, "
        "level TEXT, value REAL, unit TEXT, detail TEXT, source TEXT)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_run "
                 f"ON {TABLE} (config_name, run_id)")

    written = 0
    for name, check in (checks or {}).items():
        try:
            result = check(conn)
        except Exception as exc:  # noqa: BLE001 - one bad plugin must not stop the rest
            logger.warning("health check %r failed: %s", name, exc)
            continue
        rows = [(r.config_name, r.run_id, r.check, r.level,
                 None if r.value is None else float(r.value),
                 r.unit, r.detail, source)
                for r in _rows_from(name, result)]
        if rows:
            conn.executemany(
                f"INSERT INTO {TABLE} (config_name, run_id, check_name, level, value, unit, "
                "detail, source) VALUES (?,?,?,?,?,?,?,?)", rows)
            written += len(rows)
    conn.commit()
    return written
