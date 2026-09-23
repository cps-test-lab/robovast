"""Config version 4 -> 5: a campaign's tables are built by the decoder, not by postprocessing steps.

A campaign's tables are built from its records the first time something names them, in the
service or wherever the campaign is read, so three things a v4 config could say about how
postprocessing produced them no longer have anything to act on:

* ``results_processing.resources`` sized the pod that converted a campaign's bags in its
  execution image. That pod does not exist; nothing is converted. The block is removed: it
  sized infrastructure, not the experiment, so no run and no table depends on it.
* ``run_log`` and ``resource_usage`` as ``postprocessing`` entries ran the steps that merged a
  run's log and sliced its resource samples. Those tables are now built for every run, like
  any other, so a bare entry is removed -- it asked for what now happens anyway.
* ``run_log`` or ``resource_usage`` with parameters (``run_log``'s ``min_severity`` kept
  only the more severe lines). A table built from the records holds every row, and every
  reading surface narrows it itself; what the entry asked for cannot be expressed, so it is
  refused with a marker rather than widened silently.

The same entries under ``search.postprocessing`` are treated the same way.

``execution.local`` goes with them: it held the overrides of a run on a developer's Docker,
and a campaign runs on a cluster, where the block applied to nothing. Like the conversion
pod's sizing it described infrastructure, not the experiment, so it is removed.

**Pure ``dict`` -> ``dict``.** Nothing here may import :mod:`robovast.common.config`;
``test_migration_purity`` enforces it. Deep-copy the input and mutate the copy -- never
``dict(raw)``, which drops a ruamel ``CommentedMap``'s comments.
"""

import copy

#: Steps whose tables are now built for every run; a bare entry naming one is removed.
_BUILT_FOR_EVERY_RUN = ("run_log", "resource_usage")


def _name_and_params(entry):
    if isinstance(entry, str):
        return entry, {}
    if isinstance(entry, dict) and len(entry) == 1:
        name = next(iter(entry))
        return name, entry[name] or {}
    return None, None


def _migrate_steps(steps, where: str, refused: list):
    """*steps* without the entries that name a table now built for every run."""
    from . import migration_marker  # pylint: disable=import-outside-toplevel
    kept = type(steps)() if isinstance(steps, list) else []
    for entry in steps:
        name, params = _name_and_params(entry)
        if name not in _BUILT_FOR_EVERY_RUN:
            kept.append(entry)
        elif params:
            refused.append(where)
            kept.append(migration_marker(
                f"'{name}' is a table built for every run from version 5 and takes no "
                "parameters; what they narrowed is narrowed where the table is read (every "
                "log surface takes a minimum severity)", was=entry))
    return kept


def migrate(raw: dict) -> dict:
    """Return *raw* restructured as a version 5 config. Does not mutate the input."""
    out = copy.deepcopy(raw)
    refused: list = []

    execution = out.get("execution")
    if isinstance(execution, dict):
        execution.pop("local", None)
    results = out.get("results_processing")
    if isinstance(results, dict):
        results.pop("resources", None)
        if isinstance(results.get("postprocessing"), list):
            results["postprocessing"] = _migrate_steps(
                results["postprocessing"], "results_processing.postprocessing", refused)
    search = out.get("search")
    if isinstance(search, dict) and isinstance(search.get("postprocessing"), list):
        search["postprocessing"] = _migrate_steps(
            search["postprocessing"], "search.postprocessing", refused)

    out["version"] = 5
    if refused:
        from . import UnmigratableConfig  # pylint: disable=import-outside-toplevel
        raise UnmigratableConfig(
            f"{', '.join(sorted(set(refused)))} passes parameters to a table version 5 builds "
            "for every run, which it cannot express: the table holds every row, narrowed "
            "where it is read", partial=out, reached=4, capability="run_log.min_severity")
    return out
