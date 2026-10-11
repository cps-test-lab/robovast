"""Config version 5 -> 6: a job is one run, and ``execution.runs_per_job`` is gone.

In v5 ``runs_per_job`` packed several runs into one job, and ``timeout`` was the budget of that
whole job. In v6 every run is its own job, so ``timeout`` is the budget of one run. A file that
packed ``k`` runs behind ``timeout: T`` asked for ``T`` seconds for ``k`` runs; each run is now
given ``ceil(T / k)``, the share the file allotted it.

At ``runs_per_job: 1`` the two meanings coincide, so the key is dropped and ``timeout`` is left
exactly as the author wrote it, comments and all.

**Pure ``dict`` -> ``dict``.** Nothing here may import :mod:`robovast.common.config`;
``test_migration_purity`` enforces it. Deep-copy the input and mutate the copy -- never
``dict(raw)``, which drops a ruamel ``CommentedMap``'s comments.
"""

import copy


def migrate(raw: dict) -> dict:
    """Return *raw* restructured as a version 6 config. Does not mutate the input."""
    out = copy.deepcopy(raw)

    execution = out.get("execution")
    if isinstance(execution, dict):
        runs_per_job = execution.pop("runs_per_job", None)
        # Guarded on both values being sane rather than coerced: a malformed timeout is the
        # schema's to reject, and rewriting it here would hide where it came from.
        timeout = execution.get("timeout")
        if (isinstance(timeout, int) and not isinstance(timeout, bool)
                and isinstance(runs_per_job, int) and not isinstance(runs_per_job, bool)
                and runs_per_job > 1):
            execution["timeout"] = -(-timeout // runs_per_job)

    out["version"] = 6
    return out
