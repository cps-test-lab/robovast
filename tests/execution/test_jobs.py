# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign's jobs: one per run, in a stable order."""

from robovast.execution.jobs import build_jobs


def test_every_run_is_its_own_job():
    configs = [{"name": "c1"}, {"name": "c2"}]
    jobs = build_jobs(configs, 3)

    assert [(j.config_name, j.run_number) for j in jobs] == [
        ("c1", 0), ("c1", 1), ("c1", 2), ("c2", 0), ("c2", 1), ("c2", 2)]
    assert [j.index for j in jobs] == list(range(6))


def test_the_jobs_are_the_same_on_every_call():
    """The parameter files and the manifests are built by separate calls, for the same jobs."""
    configs = [{"name": "b"}, {"name": "a"}]
    assert build_jobs(configs, 2) == build_jobs(configs, 2)
