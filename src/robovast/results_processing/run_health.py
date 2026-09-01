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

**What a check reads.** The campaign's already-derived tables in the central index -- for
nav2 that would be ``nav2_behaviors``, ``nav2_behavior_tree`` and the control-loop warnings
in ``run_log``; for MoveIt 2, planning time and solve failures. None of that is known here.

**The check contract is** ``check(conn, campaign_id)``, **and the second argument is not
optional.** The index holds every campaign in one set of tables, so a query without
``WHERE campaign_id = %s`` reads the whole corpus: a check would grade runs from campaigns
it never saw, and the rows it wrote would be attributed to this one. *campaign_id* is a
required positional parameter with no default precisely so an unported third-party check
raises rather than doing that quietly -- an arity error is recoverable, a corpus-wide
``ok`` row is not. What the arity cannot catch is a ported check that takes the argument
and forgets to use it in one of its statements; that stays the check author's
responsibility, and it is the single thing to look for when reviewing one.

Placeholders are Postgres' ``%s``, not SQLite's ``?``. Campaign dimension tables (
``campaign``, ``batch``, ``unit``, ``run``, ``job``, ``node``, ``container_failure``) live
in the ``campaign`` schema and must be written ``campaign.job``; metric tables (``runs``,
``run_log``, ...) are unqualified. ``CAST(x AS REAL)`` is a trap carried over from SQLite:
Postgres' ``real`` is 4 bytes and silently mangles epoch timestamps -- use
``double precision``.

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

from robovast.results_processing import index_schema
from robovast.results_processing.csv_types import REAL, TEXT

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
    """The health checks a campaign asked for, by name. Nothing runs undeclared.

    *declared* is ``results_processing.health_checks`` from the campaign's ``.vast``. A name
    resolves against the installed :data:`HEALTH_GROUP` entry points; a local
    ``./path.py:Class`` ref is loaded from beside the config, which is how a system under
    test ships a check for itself without packaging one.

    **An earlier version ran every installed check automatically**, on the reasoning that a
    check only reads tables that already exist, so the campaigns most in need of grading --
    the ones nobody thought about -- would otherwise be the ones without it. That is recorded
    rather than quietly dropped, because it is a real argument and it lost to a better one:
    a check that runs everywhere grades campaigns it knows nothing about. ``nav2``'s
    control-loop check finds no misses in a MoveIt 2 campaign and would write ``ok`` for every
    run of it -- a clean bill for a stack that was never there, which is exactly the
    confusion rule 2 exists to prevent, arriving through the mechanism meant to serve it.

    Declaring is also the only way the campaign record says which checks were *meant* to run.
    Without it, a missing row could mean the check was not installed on the machine that
    postprocessed, and nothing afterwards could tell that from a stack with nothing to say.
    """
    from robovast.common.plugin_ref import is_file_ref, load_ref  # noqa: PLC0415

    installed = {}
    try:
        installed = {ep.name: ep for ep in entry_points(group=HEALTH_GROUP)}
    except Exception:  # noqa: BLE001 - no entry points at all is not an error
        pass

    checks = {}
    for ref in declared or []:
        name = ref if isinstance(ref, str) else next(iter(ref))
        try:
            if is_file_ref(name):
                obj = load_ref(name, config_dir)
            elif name in installed:
                obj = installed[name].load()
            else:
                # Loud, and it must stay loud: a declared check that silently did not run
                # leaves no rows, and no rows means "not checked" -- so the campaign would
                # read as ungraded rather than as misconfigured.
                logger.warning("health check %r is not installed and is not a file "
                               "reference; skipping. Installed: %s",
                               name, ", ".join(sorted(installed)) or "none")
                continue
            checks[name] = obj() if inspect.isclass(obj) else obj
        except Exception as exc:  # noqa: BLE001 - one bad plugin must not stop the rest
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


#: The table's columns, declared rather than inferred so it has its full shape even for a
#: campaign whose checks had nothing to say. ``value`` is ``REAL`` -- ``double precision``
#: in the index -- because it carries a measure whose scale the plugin chooses.
_RUN_HEALTH_COLUMNS = {
    "check_name": TEXT, "level": TEXT, "value": REAL, "unit": TEXT,
    "detail": TEXT, "source": TEXT,
}

#: What a check that still has the pre-index signature is told. Spelled out rather than left
#: as a bare ``TypeError`` because the fix is not obvious from the traceback: the argument is
#: new, and the reason it is required is a correctness property of the shared index.
_OLD_SIGNATURE = (
    "health check %r has the OLD check(conn) signature; the contract is now "
    "check(conn, campaign_id). The index holds every campaign in one set of tables, so "
    "every statement in the check needs a `WHERE campaign_id = %%s` predicate (and "
    "Postgres `%%s` placeholders, not SQLite `?`). Skipped: its runs are recorded as NOT "
    "CHECKED rather than graded against the whole corpus."
)


def _accepts_campaign_id(check) -> bool:
    """Whether *check* can be called as ``check(conn, campaign_id)``.

    Asked before calling rather than by catching ``TypeError`` from the call, because a
    ``TypeError`` raised *inside* a correct check would otherwise be reported as a signature
    problem and send its author looking in the wrong place.
    """
    # A check is a function or a callable instance; for the latter the signature that
    # matters is its bound ``__call__``, whose ``self`` is already applied.
    target = check if inspect.isroutine(check) else getattr(check, "__call__", check)  # noqa: B004
    try:
        inspect.signature(target).bind(object(), "campaign-id")
    except (TypeError, ValueError):
        return False
    return True


def build_run_health_table(sink, conn, campaign_id: str, checks=None,
                           source: str = SOURCE_STACK) -> int:
    """Run *checks* against one campaign in the index and write their rows; return the count.

    Each check is called ``check(conn, campaign_id)`` and must scope every statement it
    issues to *campaign_id* -- see the module docstring on why that argument is required.

    **The table is created even when nothing fills it.** An absent table and an empty one say
    different things -- "this campaign predates health checks" versus "they ran and found
    nothing to say" -- and only the second is evidence. Rule 2 is about runs; this is the same
    rule one level up.

    **Nothing is dropped here.** The old per-campaign writer began by dropping the table; in
    one shared index that would take every other campaign's grades with it. Idempotence comes
    from :func:`~robovast.results_processing.index_schema.clear_campaign`, which the ingest
    runs for this campaign alone before anything is written.
    """
    rows = []
    for name, check in (checks or {}).items():
        if not _accepts_campaign_id(check):
            logger.error(_OLD_SIGNATURE, name)
            continue
        try:
            result = check(conn, campaign_id)
        except Exception as exc:  # noqa: BLE001 - one bad plugin must not stop the rest
            logger.warning("health check %r failed: %s", name, exc)
            continue
        rows.extend(
            {"config_name": r.config_name, "run_id": r.run_id, "check_name": r.check,
             "level": r.level, "value": None if r.value is None else float(r.value),
             "unit": r.unit, "detail": r.detail, "source": source}
            for r in _rows_from(name, result))

    # ``context`` carries no config_name/run_id: this table's rows each name their own run,
    # so the per-row value must win over a batch-wide one.
    return sink.write(TABLE, rows, context={},
                      types={**dict(index_schema.CONTEXT_COLUMNS), **_RUN_HEALTH_COLUMNS},
                      source=f"{campaign_id}/health")
