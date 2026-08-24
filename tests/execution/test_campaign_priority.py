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

"""Campaign priority: the ordering key that makes the OLDEST campaign finish first.

Kueue orders pending workloads by priority, then by Workload creation time. That second
key is the wrong one for a search campaign, whose batches are submitted one after
another: a long-running campaign's later batches are younger than a campaign that
started after it, so without a priority the two take turns instead of the older one
finishing first. Everything here pins the property that fixes it -- the value is
monotone in the campaign's START time, forever, so no label is ever rewritten.
"""

import pytest
import yaml

from robovast.common.errors import CampaignConfigError
from robovast.execution.cluster_execution.kubernetes_kueue import (
    CAMPAIGN_PRIORITY_JOBGROUP, KUEUE_WORKLOAD_VERSION, campaign_priority_class_manifest,
    campaign_priority_class_name, campaign_priority_value)


def test_older_campaign_outranks_younger_across_every_scale():
    """Strictly decreasing in start time -- the whole contract, at every gap that matters.

    Seconds included deliberately: two campaigns started in the same minute are the
    reported case, and a coarser key would tie them and fall back to Workload creation
    time, which is precisely the bug.
    """
    ids = [
        "nav-2026-01-01-00000000",    # the reference instant
        "nav-2026-08-24-101500",      # legacy 6-digit id, months later
        "nav-2026-08-24-10150112",    # one second younger
        "nav-2026-08-24-103000",      # minutes younger
        "nav-2026-08-25-101500",      # a day younger
        "nav-2027-08-24-101500",      # a year younger
    ]
    values = [campaign_priority_value(c) for c in ids]
    assert values == sorted(values, reverse=True), values
    assert len(set(values)) == len(values), "distinct start seconds must not collide"


def test_same_second_campaigns_tie():
    """Sub-second precision is deliberately dropped, and that is a real tie.

    Campaign ids carry hundredths so two launches in the same second get distinct
    DIRECTORIES; the priority only needs to separate campaigns, and a second is finer
    than any realistic gap between two `start_campaign` calls a human makes. Two
    campaigns that genuinely start in the same second fall back to Kueue's second key,
    Workload creation time -- today's behaviour, for that pair alone. Pinned so the
    coarsening is a decision on record rather than a surprise.
    """
    assert (campaign_priority_value("nav-2026-08-24-10150012")
            == campaign_priority_value("nav-2026-08-24-10150099")
            == campaign_priority_value("nav-2026-08-24-101500"))


def test_value_stays_within_int32():
    """The CRD types ``value`` as int32; a wrapped value would silently invert order."""
    for cid in ("nav-2026-01-01-000000", "nav-2089-12-31-235959"):
        assert -2**31 <= campaign_priority_value(cid) < 2**31


def test_value_is_immune_to_the_dst_fall_back_hour():
    """Monotone in the wall-clock label, not in epoch seconds.

    Going through ``.timestamp()`` folds the repeated hour of a DST fall-back onto
    itself, which would tie -- or invert -- two campaigns started inside it. Europe's
    2026 fall-back is 2026-10-25 03:00 -> 02:00, so these two labels are one hour apart
    on the clock but can map to the same epoch second.
    """
    earlier = campaign_priority_value("nav-2026-10-25-020000")
    later = campaign_priority_value("nav-2026-10-25-023000")
    assert earlier > later


def test_malformed_campaign_id_is_refused_not_defaulted():
    """A missing timestamp must raise, never quietly become the lowest priority.

    Defaulting would park that campaign behind every other one for as long as it ran,
    with nothing said -- the silent degradation this priority exists to prevent.
    """
    with pytest.raises(CampaignConfigError, match="no parsable"):
        campaign_priority_value("campaign-without-a-timestamp")


def test_manifest_round_trips_and_carries_the_cleanup_labels():
    """The class is removed by the ORDINARY campaign-scoped cleanup selector.

    It carries the same ``jobgroup``/``campaign-id`` pair as the campaign's Jobs and
    Pods, which is what lets cleanup delete it with one more label selector instead of a
    GC pass of its own. Asserted through a real serialise/parse round trip.
    """
    cid = "nav-2026-08-24-10150012"
    doc = yaml.safe_load(yaml.safe_dump(campaign_priority_class_manifest(cid)))

    assert doc["kind"] == "WorkloadPriorityClass"
    assert doc["apiVersion"] == f"kueue.x-k8s.io/{KUEUE_WORKLOAD_VERSION}"
    assert doc["metadata"]["name"] == campaign_priority_class_name(cid)
    assert doc["metadata"]["labels"] == {"jobgroup": CAMPAIGN_PRIORITY_JOBGROUP,
                                         "campaign-id": cid}
    # `value` is top-level on a WorkloadPriorityClass -- there is no `spec`.
    assert doc["value"] == campaign_priority_value(cid)
    assert "spec" not in doc


def test_class_name_is_a_valid_object_name_even_for_a_long_campaign():
    name = campaign_priority_class_name("A_Very" + "-Long" * 60 + "-2026-08-24-101500")
    assert len(name) <= 253
    assert name == name.lower()
    assert all(c.isalnum() or c in "-." for c in name)


def _cleanup(campaign):
    """Run the priority-class cleanup against a doubled API; return the call kwargs."""
    from unittest import mock

    from robovast.execution.cluster_execution import kubernetes_kueue

    api = mock.Mock()
    with mock.patch.object(kubernetes_kueue.client, "CustomObjectsApi",
                           return_value=api):
        kubernetes_kueue.cleanup_campaign_priority_classes(campaign=campaign)
    return api.delete_collection_cluster_custom_object.call_args.kwargs


def test_campaign_cleanup_is_scoped_to_that_campaign():
    """One campaign's cleanup must not delete a concurrent campaign's priority class.

    Prioritization only exists because campaigns run concurrently, so an unscoped delete
    here would strip a running campaign of its priority -- and Kueue rejects that
    campaign's next Job outright, since its label would name a class that is gone.
    """
    selector = _cleanup("nav-2026-08-24-101500")["label_selector"]
    assert selector == "jobgroup=campaign-priority,campaign-id=nav-2026-08-24-101500"


def test_full_cleanup_takes_every_campaign_priority_class():
    """With no campaign given, the selector still names the jobgroup -- so a priority
    class an operator wrote by hand is left alone."""
    selector = _cleanup(None)["label_selector"]
    assert selector == "jobgroup=campaign-priority"


def test_cleanup_survives_a_cluster_without_the_crd():
    """A 404 means Kueue has no WorkloadPriorityClass kind; teardown must not fail on it.

    A leftover class is inert once the campaign's jobs are gone, so failing a campaign's
    cleanup over one would be worse than the litter it prevents.
    """
    from unittest import mock

    from kubernetes.client import rest

    from robovast.execution.cluster_execution import kubernetes_kueue

    api = mock.Mock()
    api.delete_collection_cluster_custom_object.side_effect = rest.ApiException(
        status=404, reason="Not Found")
    with mock.patch.object(kubernetes_kueue.client, "CustomObjectsApi",
                           return_value=api):
        kubernetes_kueue.cleanup_campaign_priority_classes(campaign="nav-2026-08-24-101500")
