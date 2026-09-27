# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The ``job`` symlinks a campaign's manifest describes stay inside the campaign.

The manifest is a file in the campaign's ``_transient/``, and creating a link removes what
is at its path first, so an entry that leads out would delete and replace a file anywhere
the driver can write.
"""

import pytest
import yaml

from robovast.client.safe_path import UnsafePathError
from robovast.common.execution import JOB_LINKS_MANIFEST, create_job_links


def _campaign(tmp_path, links):
    root = tmp_path / "camp-2026-09-21-120000"
    (root / "_transient").mkdir(parents=True)
    (root / "_transient" / JOB_LINKS_MANIFEST).write_text(yaml.safe_dump(links))
    return root


def test_the_manifests_links_are_created(tmp_path):
    root = _campaign(tmp_path, {"cfg/0/job": "../../_jobs/batch-0/job-0"})
    assert create_job_links(root) == 1
    assert (root / "cfg" / "0" / "job").readlink().as_posix() == "../../_jobs/batch-0/job-0"


@pytest.mark.parametrize("links", [
    {"../outside/job": "../../_jobs/batch-0/job-0"},
    {"/tmp/outside/job": "../../_jobs/batch-0/job-0"},
    {"cfg/0/..": "../../_jobs/batch-0/job-0"},
    {"cfg/0/job": "../../../elsewhere"},
    {"cfg/0/job": "/etc"},
])
def test_a_link_leading_out_of_the_campaign_is_refused_before_anything_is_touched(
        tmp_path, links):
    root = _campaign(tmp_path, {"cfg/1/job": "../../_jobs/batch-0/job-1", **links})
    victim = tmp_path / "outside" / "job"
    victim.parent.mkdir()
    victim.write_text("not the campaign's")
    with pytest.raises(UnsafePathError):
        create_job_links(root)
    assert victim.read_text() == "not the campaign's"
    assert not (root / "cfg" / "1" / "job").exists()


def test_a_link_directory_that_is_a_symlink_out_is_refused(tmp_path):
    root = _campaign(tmp_path, {"cfg/0/job": "../../_jobs/batch-0/job-0"})
    (tmp_path / "outside").mkdir()
    (root / "cfg").symlink_to(tmp_path / "outside")
    with pytest.raises(UnsafePathError):
        create_job_links(root)
