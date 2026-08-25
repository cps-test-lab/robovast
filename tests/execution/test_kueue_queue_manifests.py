# Copyright (C) 2025 Frederik Pasch
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

"""The Kueue queue manifests, rendered.

Nothing exercised these documents before, which is exactly why they carried two bugs at
once: the ResourceFlavor opened ``spec:`` for its tolerations and the node-label helper
appended a *second* ``spec:`` key, so PyYAML's last-wins parse silently dropped the
toleration; and label values were emitted as bare YAML scalars where Kubernetes requires
strings. Both are asserted here through a real serialise/parse round trip, because that
is the only way either would have been visible.
"""

import yaml

from robovast.execution.cluster_execution.kubernetes_kueue import (CLUSTER_QUEUE_NAME,
                                                                   KUEUE_QUEUE_NAME,
                                                                   KUEUE_RESOURCE_FLAVOR_NAME,
                                                                   _queue_manifests)


def _round_trip(**kwargs):
    """Serialise the manifests and parse them back, as ``apply_kueue_queues`` does."""
    kwargs.setdefault("namespace", "default")
    kwargs.setdefault("queue_name", KUEUE_QUEUE_NAME)
    kwargs.setdefault("cluster_queue", CLUSTER_QUEUE_NAME)
    kwargs.setdefault("cpu_quota", 96)
    kwargs.setdefault("memory_quota", "125Gi")
    text = yaml.safe_dump_all(_queue_manifests(**kwargs), default_flow_style=False,
                             sort_keys=False)
    docs = list(yaml.safe_load_all(text))
    return text, {d["kind"]: d for d in docs}


def test_the_trio_is_emitted():
    _, by_kind = _round_trip()
    assert set(by_kind) == {"ResourceFlavor", "ClusterQueue", "LocalQueue"}
    assert by_kind["LocalQueue"]["metadata"]["namespace"] == "default"
    assert by_kind["LocalQueue"]["spec"]["clusterQueue"] == CLUSTER_QUEUE_NAME
    assert by_kind["ResourceFlavor"]["metadata"]["name"] == KUEUE_RESOURCE_FLAVOR_NAME


def test_node_labels_do_not_displace_the_toleration():
    """The regression. The batch toleration and the node labels must coexist: losing the
    toleration silently makes every job unschedulable on a tainted batch node pool."""
    _, by_kind = _round_trip(node_labels={"node-pool": "primary"})
    spec = by_kind["ResourceFlavor"]["spec"]
    assert spec["tolerations"] == [
        {"key": "dedicated", "value": "batch", "effect": "NoSchedule"}]
    assert spec["nodeLabels"] == {"node-pool": "primary"}


def test_node_label_values_are_strings():
    """Kubernetes rejects a non-string label value, so a bare ``true`` or ``3`` in a
    ``.vast`` must not reach the API server as a YAML bool or int."""
    text, by_kind = _round_trip(node_labels={"robovast": True, "replicas": 3})
    assert by_kind["ResourceFlavor"]["spec"]["nodeLabels"] == {
        "robovast": "True", "replicas": "3"}
    assert "robovast: 'True'" in text


def test_without_node_labels_the_flavor_carries_only_tolerations():
    _, by_kind = _round_trip()
    assert list(by_kind["ResourceFlavor"]["spec"]) == ["tolerations"]


def test_quota_covers_cpu_and_memory():
    _, by_kind = _round_trip()
    group = by_kind["ClusterQueue"]["spec"]["resourceGroups"][0]
    assert group["coveredResources"] == ["cpu", "memory"]
    assert group["flavors"][0]["resources"] == [
        {"name": "cpu", "nominalQuota": 96},
        {"name": "memory", "nominalQuota": "125Gi"}]


# --- The priority-class name is a LABEL VALUE, so 63 is the limit that binds ------------
#
# It is also the WorkloadPriorityClass's object name, where 253 would be legal -- and the
# Jobs reference the class by carrying it under KUEUE_PRIORITY_LABEL. Capping at 253 made
# the API server refuse every Job of a campaign whose id ran past 45 characters, at Job
# creation, after the image build and the whole variation phase
# (optuna-pilot-fullmigration-2026-08-25-01000676: 64 characters, one over).

def test_a_long_campaign_id_still_yields_a_usable_label_value():
    from robovast.execution.cluster_execution.kubernetes_kueue import \
        campaign_priority_class_name
    name = campaign_priority_class_name(
        "optuna-pilot-fullmigration-2026-08-25-01000676")
    assert len(name) <= 63, name


def test_an_ordinary_campaign_id_is_left_alone():
    """The digest is for the ids that need it; a normal one must read as itself."""
    from robovast.execution.cluster_execution.kubernetes_kueue import \
        campaign_priority_class_name
    assert campaign_priority_class_name("nav-2026-08-25-01004390") == \
        "robovast-campaign-nav-2026-08-25-01004390"


def test_two_long_ids_do_not_collapse_onto_one_class():
    """Truncating flat would be worse than the error it fixes: two campaigns would share a
    priority, and the campaign-scoped cleanup would delete the other one's class."""
    from robovast.execution.cluster_execution.kubernetes_kueue import \
        campaign_priority_class_name
    a = campaign_priority_class_name("a-very-long-campaign-name-alpha-2026-08-25-010043")
    b = campaign_priority_class_name("a-very-long-campaign-name-beta-2026-08-25-010043")
    assert a != b
    assert len(a) <= 63 and len(b) <= 63


def test_the_value_never_ends_in_a_separator():
    """A label value must start and end alphanumeric, and a cut lands on a hyphen often."""
    from robovast.execution.cluster_execution.kubernetes_kueue import \
        campaign_priority_class_name
    for n in range(40, 60):
        value = campaign_priority_class_name("x" * n + "-2026-08-25-010043")
        assert value[-1].isalnum(), value
        assert len(value) <= 63
