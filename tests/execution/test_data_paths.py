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

"""Where each tenant lands, and which settings are refused rather than resolved."""

import pytest

from robovast.execution.cluster_execution import data_paths


def test_the_defaults_are_the_paths_a_deployment_already_uses():
    """The defaults are load-bearing, not cosmetic.

    A cluster set up after this module existed must land exactly where it landed before it
    did. A changed default would point a live deployment at a fresh empty directory beside
    its own data, and report success doing it.
    """
    defaults = {t.name: t.default_path for t in data_paths.TENANTS}
    assert defaults == {
        "workspaces": "/var/lib/robovast-workspaces",
        "results": "/var/lib/robovast-results",
        "registry": "/var/lib/robovast-registry",
        "buildkit": "/data/robovast-buildkit",
    }


def test_stating_nothing_places_nothing_so_a_re_run_keeps_what_the_cluster_has():
    """An unplaced tenant must come back empty rather than defaulted.

    A plain ``setup`` re-run states no paths. If this filled in the defaults, that run would
    hand a deployment its default *explicitly*, outranking the recovery that keeps a moved
    deployment on its own data -- and move it, reporting success.
    """
    resolved = data_paths.resolve()
    assert all(p.path == "" for p in resolved.values())
    assert all(p.storage_class == "" for p in resolved.values())


def test_a_root_places_every_tenant_that_was_not_named():
    resolved = data_paths.resolve(data_root="/media/data")
    assert resolved["workspaces"].path == "/media/data/workspaces"
    assert resolved["registry"].path == "/media/data/registry"
    assert resolved["buildkit"].path == "/media/data/buildkit"


def test_a_named_tenant_overrides_the_root_and_the_rest_still_follow_it():
    resolved = data_paths.resolve({"buildkit_path": "/ssd/cache"}, data_root="/media/data")
    assert resolved["buildkit"].path == "/ssd/cache"
    assert resolved["workspaces"].path == "/media/data/workspaces"


@pytest.mark.parametrize("parent, expected", [
    ("/var/lib/robovast-workspaces", "/var/lib/robovast-results"),
    ("/media/data/workspaces", "/media/data/results"),
    ("/mnt/ws", "/mnt/ws-results"),
])
def test_results_sits_beside_the_workspaces_whatever_they_are_called(parent, expected):
    """A derived tenant keeps its parent's naming convention rather than being given ours."""
    resolved = data_paths.resolve({"workspaces_path": parent})
    assert resolved["results"].path == expected


def test_a_derived_tenant_takes_its_parent_backing_so_the_two_cannot_split():
    """Results is a claim exactly where workspaces is one.

    The service pod mirrors a campaign between the two, so one on a provisioned volume and
    one on a node directory would pin the pod for the sake of a volume that does not need it
    -- and abandon the other half on whichever node it was.
    """
    resolved = data_paths.resolve({"workspaces_class": "fast-ssd"})
    assert resolved["results"].storage_class == "fast-ssd"
    assert not resolved["results"].is_node_local


def test_a_root_places_the_tenant_so_it_outranks_recovery_downstream():
    """Recovery answers for a caller that said nothing; a root is a caller saying where.

    Recovery is applied further down, by the deploy path that can read the live cluster, as
    ``stated or recovered``. So a root wins there exactly by producing a path here -- and if
    it produced none, it would move the registry and the build cache while leaving the
    workspaces behind, which is the split placement this resolver exists to prevent.
    """
    assert data_paths.resolve(data_root="/media/data")["workspaces"].path == \
        "/media/data/workspaces"


def test_a_path_and_a_class_for_one_tenant_are_refused_not_silently_ordered():
    """A class wins over a path, so accepting both places the tenant somewhere unasked."""
    with pytest.raises(ValueError) as excinfo:
        data_paths.refuse_conflicts({"registry_path": "/media/data/registry",
                                     "registry_class": "local-path"})
    assert "--registry-path" in str(excinfo.value)
    assert "--registry-class" in str(excinfo.value)


def test_a_root_beside_a_class_is_not_a_conflict():
    """The root places the node-local tenants; a class is how one opts out of being one."""
    data_paths.refuse_conflicts({"workspaces_class": "fast-ssd"}, data_root="/media/data")


def test_a_size_without_a_class_is_refused():
    with pytest.raises(ValueError) as excinfo:
        data_paths.refuse_conflicts({}, sizes={"buildkit": "200Gi"})
    assert "--buildkit-size" in str(excinfo.value)
    assert "--buildkit-class" in str(excinfo.value)


def test_a_relative_root_is_refused_because_the_kubelet_resolves_it():
    with pytest.raises(ValueError) as excinfo:
        data_paths.refuse_conflicts({}, data_root="media/data")
    assert "absolute" in str(excinfo.value)


def test_only_placeable_tenants_take_flags():
    """A flag for a derived tenant could only agree with its parent or be refused."""
    assert "results" not in data_paths.PLACEABLE
    assert set(data_paths.PLACEABLE) == {"workspaces", "registry", "buildkit"}
