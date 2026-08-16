# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``job_links.yaml``, the only way a run's job artifacts can be found.

``iter_run_slices`` resolves a run's job through this manifest and deliberately never through
the ``job`` symlink (a symlink cannot exist in an object store). So a missing entry is not a
cosmetic problem: that run gets no ``run_log`` and no ``resource_usage``, and reports only
"no job_links entry".

The manifest is **campaign-level** while it is written **per batch**, which is the whole
hazard here. A four-batch campaign was found holding entries for its last two batches only.
"""

import pytest
import yaml

from robovast.common.execution import JOB_LINKS_MANIFEST, read_job_links, write_job_links_manifest


class _Item:
    def __init__(self, config_name, run_number):
        self.config_name = config_name
        self.run_number = run_number


class _Job:
    """The shape ``build_job_links`` reads: an index and the work items packed into it."""

    def __init__(self, index, items):
        self.index = index
        self.items = items


def _job(index, *configs):
    return _Job(index, [_Item(c, 0) for c in configs])


def _write(campaign, jobs, prefix):
    write_job_links_manifest(str(campaign / "_transient"), jobs, prefix,
                             base=read_job_links(str(campaign)))


def test_a_later_batch_keeps_the_entries_of_an_earlier_one(tmp_path):
    """The bug: each batch is a separate call, and the manifest is shared by all of them."""
    _write(tmp_path, [_job(0, "cfg-a")], "batch-0")
    _write(tmp_path, [_job(0, "cfg-b")], "batch-1")
    links = read_job_links(str(tmp_path))
    assert links == {
        "cfg-a/0/job": "../../_jobs/batch-0/job-0",
        "cfg-b/0/job": "../../_jobs/batch-1/job-0",
    }


def test_rewriting_the_same_batch_is_idempotent(tmp_path):
    """A retried or resumed batch writes the same targets, which must not look like a clash."""
    _write(tmp_path, [_job(0, "cfg-a")], "batch-0")
    _write(tmp_path, [_job(0, "cfg-a")], "batch-0")
    assert read_job_links(str(tmp_path)) == {"cfg-a/0/job": "../../_jobs/batch-0/job-0"}


def test_a_run_reassigned_to_another_job_raises(tmp_path):
    """One run's artifacts cannot live in two jobs. Choosing a winner quietly is how a run
    ends up resolving to a different batch's log -- the failure this manifest causes when it
    is wrong is a *plausible* one, so it has to be loud here."""
    _write(tmp_path, [_job(0, "cfg-a")], "batch-0")
    with pytest.raises(ValueError, match="conflicting"):
        _write(tmp_path, [_job(0, "cfg-a")], "batch-1")


def test_without_a_base_only_the_current_batch_is_written(tmp_path):
    """The documented behaviour of omitting *base*, which the one-shot template dir wants:
    no campaign to accumulate onto. Pinned so nobody 'fixes' it into an implicit read."""
    write_job_links_manifest(str(tmp_path / "_transient"), [_job(0, "cfg-a")], "batch-0")
    write_job_links_manifest(str(tmp_path / "_transient"), [_job(0, "cfg-b")], "batch-1")
    assert read_job_links(str(tmp_path)) == {"cfg-b/0/job": "../../_jobs/batch-1/job-0"}


def test_an_unprefixed_write_replaces_because_its_job_index_is_not_stable(tmp_path):
    """The single-batch default: one call writes the whole campaign, and ``_jobs/job-<idx>``
    is only meaningful within it. Re-running or re-packing moves ``cfg-b/0`` from ``job-1`` to
    ``job-0``, so accumulating would aim it at ``cfg-a``'s artifacts. Replacing is what this
    path has always done, and it must stay that way."""
    _write(tmp_path, [_job(0, "cfg-a"), _job(1, "cfg-b")], "")
    assert read_job_links(str(tmp_path))["cfg-b/0/job"] == "../../_jobs/job-1"
    _write(tmp_path, [_job(0, "cfg-b")], "")   # re-packed: cfg-b is job-0 now
    assert read_job_links(str(tmp_path)) == {"cfg-b/0/job": "../../_jobs/job-0"}


def test_a_packed_job_links_every_run_it_serves(tmp_path):
    """runs_per_job > 1: several configurations share one job dir, so several links point at
    it. This is what makes one log belong to several runs, and it must survive the merge."""
    _write(tmp_path, [_job(0, "cfg-a", "cfg-b", "cfg-c")], "batch-0")
    assert read_job_links(str(tmp_path)) == {
        "cfg-a/0/job": "../../_jobs/batch-0/job-0",
        "cfg-b/0/job": "../../_jobs/batch-0/job-0",
        "cfg-c/0/job": "../../_jobs/batch-0/job-0",
    }


def test_nothing_is_written_when_there_is_nothing_to_write(tmp_path):
    """A single-config campaign has no ``_jobs`` split and therefore no manifest at all."""
    write_job_links_manifest(str(tmp_path / "_transient"), [], "")
    assert not (tmp_path / "_transient" / JOB_LINKS_MANIFEST).exists()


def test_the_manifest_stays_plain_sorted_data(tmp_path):
    """It has to survive an S3 round-trip and be diffable, so: plain keys, sorted."""
    _write(tmp_path, [_job(0, "cfg-b")], "batch-0")
    _write(tmp_path, [_job(0, "cfg-a")], "batch-1")
    text = (tmp_path / "_transient" / JOB_LINKS_MANIFEST).read_text()
    assert list(yaml.safe_load(text)) == ["cfg-a/0/job", "cfg-b/0/job"]
    assert text.startswith("cfg-a/0/job:")
