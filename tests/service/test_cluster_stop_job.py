# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Stopping one Job of a running cluster campaign: record why, then delete it.

The ledger is written into the campaign on the service's results volume, which is where
the campaign's postprocessing reads it; nothing has to be copied anywhere first.
"""

import threading

from robovast.common.campaign_data import read_interventions
from robovast.execution.cluster_execution.cluster_service import ClusterService

_CAMPAIGN = "camp-2026-09-01-120000"


class _Batch:
    def __init__(self):
        self.deleted = []

    def delete_namespaced_job(self, name, namespace, **kwargs):
        self.deleted.append((name, namespace, kwargs.get("propagation_policy")))


def _service(tmp_path, batch):
    service = ClusterService.__new__(ClusterService)
    service.namespace = "ns"
    service._lock = threading.Lock()                               # pylint: disable=protected-access
    service._campaigns = {_CAMPAIGN: object()}                     # pylint: disable=protected-access
    service._campaigns_root = lambda: tmp_path                     # pylint: disable=protected-access
    service._require_running_job = lambda campaign_id, job: None   # pylint: disable=protected-access
    service._job_artifact_dir = lambda job: "_jobs/batch-0/job-3"  # pylint: disable=protected-access
    service._k8s_batch = lambda: batch                             # pylint: disable=protected-access
    (tmp_path / _CAMPAIGN / "_execution").mkdir(parents=True)
    return service


def test_a_stopped_job_is_recorded_in_the_campaign_and_deleted(tmp_path):
    batch = _Batch()
    result = _service(tmp_path, batch).stop_job(_CAMPAIGN, "camp-batch-0-job-3",
                                                reason="wedged", source="cli")
    assert result.ok, result.message
    assert batch.deleted == [("camp-batch-0-job-3", "ns", "Background")]
    entry, = read_interventions(tmp_path / _CAMPAIGN)
    assert entry["job_dir"] == "_jobs/batch-0/job-3" and entry["kind"] == "killed"
