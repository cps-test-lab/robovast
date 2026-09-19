# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""How much room the campaigns get, and what raising it is allowed to do.

The results volume is where every campaign lives, so its size is the one storage question
an operator comes back to after setup. A claim keeps the size it was created with, which
is why growing it is a patch of its own rather than a re-render.
"""

import types

import pytest

from robovast.execution.cluster_execution import data_paths, service_deploy


class _ApiException(Exception):
    def __init__(self, status):
        super().__init__(str(status))
        self.status = status


class _Core:
    """Just enough CoreV1Api to read one claim and record the patch it is sent."""

    def __init__(self, size=None):
        self.size = size
        self.patched = None

    def read_namespaced_persistent_volume_claim(self, name, namespace):
        assert name == service_deploy.RESULTS_VOLUME_NAME
        if self.size is None:
            raise _ApiException(404)
        return types.SimpleNamespace(
            spec=types.SimpleNamespace(
                resources=types.SimpleNamespace(requests={"storage": self.size})))

    def patch_namespaced_persistent_volume_claim(self, name, namespace, body, dry_run=None):
        self.patched = body


@pytest.fixture(autouse=True)
def _kube_exceptions(monkeypatch):
    """``grow_results_claim`` catches the kubernetes client's ApiException by import."""
    monkeypatch.setitem(
        __import__("sys").modules, "kubernetes.client.rest",
        types.SimpleNamespace(ApiException=_ApiException))
    yield


def test_a_deployment_that_states_no_size_gets_one_large_enough_to_hold_campaigns():
    """The default is the claim's, not an empty request the class would reject."""
    pvc = service_deploy.results_pvc_manifest("default", "fast")
    assert pvc["spec"]["resources"]["requests"]["storage"] == service_deploy.DEFAULT_RESULTS_SIZE


def test_the_claim_is_absent_where_the_results_are_a_node_directory():
    """A hostPath deployment has no volume, so rendering one would leave a pod Pending."""
    assert service_deploy.results_pvc_manifest("default", "") is None


def test_a_size_without_a_class_is_refused_before_anything_is_applied():
    """Sizing a claim nothing creates reads as configured and is not."""
    with pytest.raises(ValueError, match="results-size"):
        data_paths.refuse_conflicts({}, sizes={"results": "1Ti"})


def test_a_size_under_the_workspaces_class_passes_because_that_claim_is_created():
    """results is derived: its backing is the workspaces' class, so the claim exists."""
    data_paths.refuse_conflicts({"workspaces_class": "fast"}, sizes={"results": "1Ti"})


def test_growing_patches_only_the_request_so_a_bound_claim_is_not_replaced():
    core = _Core("500Gi")
    message = service_deploy.grow_results_claim(core, "default", "1Ti")
    assert core.patched == {"spec": {"resources": {"requests": {"storage": "1Ti"}}}}
    assert "1Ti" in message


def test_the_same_size_twice_changes_nothing():
    """An upgrade that carries the standing flag must not patch on every run."""
    core = _Core("500Gi")
    service_deploy.grow_results_claim(core, "default", "500Gi")
    assert core.patched is None


def test_a_smaller_size_is_refused_rather_than_sent():
    """Kubernetes rejects a shrink; sending it would lose the operator's intent in a 422."""
    core = _Core("1Ti")
    with pytest.raises(RuntimeError, match="cannot be shrunk"):
        service_deploy.grow_results_claim(core, "default", "500Gi")
    assert core.patched is None


def test_units_are_compared_as_bytes_so_the_two_series_do_not_read_as_a_shrink():
    """``750G`` is more than ``500Gi``; comparing the strings would call it less."""
    core = _Core("500Gi")
    service_deploy.grow_results_claim(core, "default", "750G")
    assert core.patched["spec"]["resources"]["requests"]["storage"] == "750G"


def test_a_deployment_with_no_claim_is_told_so_instead_of_reporting_a_resize():
    core = _Core(None)
    with pytest.raises(RuntimeError, match="directory on the data node"):
        service_deploy.grow_results_claim(core, "default", "1Ti")


def test_an_unreadable_size_is_refused_naming_what_was_read():
    with pytest.raises(ValueError, match="not a storage size"):
        service_deploy.parse_quantity("plenty")
