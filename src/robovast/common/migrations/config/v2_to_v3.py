"""Config version 2 -> 3: ``execution.timeout`` becomes the job's budget, and
``execution.bt_log`` / ``execution.log_topics`` are gone.

``timeout`` meant "one run", and both lanes multiplied it by ``runs_per_job`` to get the
figure they could actually enforce. Neither lane can bound an individual run inside a
packed job -- the cluster sets ``activeDeadlineSeconds`` on the Job, and the local lane
wraps a whole compose step -- so the per-run figure was a unit nothing enforced,
reconstructed by multiplication in two places. In v3 the declared number *is* the job's
budget, which is why a v2 file has to be multiplied through here: a campaign that packed
100 runs behind ``timeout: 600`` asked for 60000 seconds of Job, and must keep asking for
it.

``bt_log`` and ``log_topics`` are dropped rather than carried. Both are removed from the
schema: behaviour-tree logging is now always on, and what a run records beyond
``/rosout`` + ``/clock`` is the scenario's ``bag_record`` to decide, not the execution
block's. **A file that set ``bt_log: false`` gains behaviour-tree logging**, which is a
change in what the run produces -- stated here because it is the one thing this step does
that a reader would not predict. It is intended: the knob is gone, not relocated.

**Pure ``dict`` -> ``dict``.** Nothing here may import :mod:`robovast.common.config`;
``test_migration_purity`` enforces it. Deep-copy the input and mutate the copy -- never
``dict(raw)``, which drops a ruamel ``CommentedMap``'s comments.
"""

import copy

#: Dropped outright: removed from the schema in v3, with no v3 spelling to carry them to.
_REMOVED_KEYS = ("bt_log", "log_topics")


def migrate(raw: dict) -> dict:
    """Return *raw* restructured as a version 3 config. Does not mutate the input."""
    out = copy.deepcopy(raw)

    execution = out.get("execution")
    if isinstance(execution, dict):
        for key in _REMOVED_KEYS:
            execution.pop(key, None)

        # Only meaningful where the campaign packed runs; at the default of 1 the two
        # meanings coincide and the number is left exactly as the author wrote it, comments
        # and all. Guarded on both values being sane rather than coerced: a malformed
        # timeout is the schema's to reject, and rewriting it here would hide where it came
        # from.
        timeout = execution.get("timeout")
        runs_per_job = execution.get("runs_per_job")
        if (isinstance(timeout, int) and not isinstance(timeout, bool)
                and isinstance(runs_per_job, int) and not isinstance(runs_per_job, bool)
                and runs_per_job > 1):
            execution["timeout"] = timeout * runs_per_job

    out["version"] = 3
    return out
