# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A rewrite of ``execution.yaml`` must not know less than the write before it.

The file is rewritten rather than appended to, and a RESUME rewrites it after the campaign's
pods have been reaped -- so the per-container digests it would read back are gone. Emitting
those keys only when there is something to put in them then means the rewrite *deletes* what
the first run recorded.

That is not hypothetical: a service restart resumed several campaigns, every one of them had
its ``image_revisions`` block erased in the same two-minute window, and every one of them
became un-retriggerable as a result -- while the two campaigns that ran long enough afterwards
to write the block again were fine.

The other half matters just as much: a field describing THIS execution must still be
overwritten, or one record starts describing two runs.
"""

import pytest
import yaml

from robovast.common import execution as execution_mod
from robovast.common.execution import create_execution_yaml

DIGEST = "reg.example/robovast@sha256:" + "a" * 64
SIM_DIGEST = "reg.example/sim@sha256:" + "b" * 64


@pytest.fixture(autouse=True)
def _no_probes(monkeypatch):
    """Keep the unit under test off docker and off a cluster.

    Both are probed with multi-second timeouts per call, and neither has anything to do with
    what these tests assert -- which is purely how a rewrite treats the record already on disk.
    """
    monkeypatch.setattr(execution_mod, "_get_image_revision", lambda image: "unknown")
    monkeypatch.setattr(execution_mod, "_get_cluster_info", lambda context=None: None)
    monkeypatch.setattr(execution_mod, "image_build_refs", lambda *a, **kw: {})


def _read(root):
    with open(root / "_execution" / "execution.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _params():
    return {"containers": {"sut": {"image": "reg.example/robovast:latest"}}}


def test_a_resume_that_can_read_no_digests_keeps_the_recorded_ones(tmp_path):
    create_execution_yaml(3, str(tmp_path), execution_params=_params(),
                          image_digest=DIGEST,
                          image_digests={"sut": DIGEST, "simulation": SIM_DIGEST})
    assert _read(tmp_path)["image_revisions"] == {"sut": DIGEST, "simulation": SIM_DIGEST}

    # The resume: same campaign, no pods left to read, so nothing to record.
    create_execution_yaml(3, str(tmp_path), execution_params=_params(),
                          image_digest=None, image_digests=None)
    after = _read(tmp_path)
    assert after["image_revisions"] == {"sut": DIGEST, "simulation": SIM_DIGEST}, (
        "the resume erased what the first run recorded")
    assert after["image_revision"] == DIGEST


def test_a_rewrite_that_knows_better_still_wins(tmp_path):
    create_execution_yaml(3, str(tmp_path), execution_params=_params(),
                          image_digests={"sut": DIGEST})
    create_execution_yaml(3, str(tmp_path), execution_params=_params(),
                          image_digests={"sut": SIM_DIGEST})
    assert _read(tmp_path)["image_revisions"] == {"sut": SIM_DIGEST}


def test_fields_describing_this_execution_are_not_carried_forward(tmp_path):
    """Carrying these would make one record describe two runs."""
    create_execution_yaml(3, str(tmp_path), execution_params=_params(), image_digest=DIGEST)
    first = _read(tmp_path)
    create_execution_yaml(7, str(tmp_path), execution_params=_params(), image_digest=DIGEST)
    second = _read(tmp_path)
    assert second["runs"] == 7
    assert second["execution_time"] != first["execution_time"]


def test_the_unknown_placeholder_does_not_count_as_a_value(tmp_path):
    """The subtle half, and the one that bit first.

    ``_get_image_revision`` returns the literal string ``'unknown'`` when it cannot read an
    image, so a resume's ``image_revision`` is *truthy* while carrying no fact at all. A
    carry-forward that only fills falsy fields would let that placeholder overwrite a recorded
    digest -- the very erasure it exists to prevent.
    """
    create_execution_yaml(3, str(tmp_path), execution_params=_params(), image_digest=DIGEST)
    assert _read(tmp_path)["image_revision"] == DIGEST

    # image_digest=None makes create_execution_yaml fall back to the probe, which is stubbed
    # to the same 'unknown' it returns off-cluster.
    create_execution_yaml(3, str(tmp_path), execution_params=_params(), image_digest=None)
    assert _read(tmp_path)["image_revision"] == DIGEST
