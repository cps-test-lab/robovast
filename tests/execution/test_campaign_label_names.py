# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Names derived from a campaign id stay distinct when they have to be shortened.

A campaign id is ``<name>-<date>-<time>``, so two runs of one experiment differ only at the
end. A name cut to fit Kubernetes' 63 characters loses exactly that end: two campaigns then
share a token Secret -- the second one's pods read the first one's token, scoped to the
first campaign, and are refused their own inputs -- and share a ``campaign-id`` label, so
cleaning up one campaign's jobs deletes the other's.
"""

from robovast.execution.cluster_execution import pod_access
from robovast.execution.cluster_execution.cluster_execution import _label_safe_campaign

_NAME = "metamorphic-pt1-big-map-remove-recursive"
_EARLIER = f"{_NAME}-2026-10-04-04111039"
_LATER = f"{_NAME}-2026-10-04-04240269"
_LONG = "a-very-long-experiment-name-that-alone-nearly-fills-a-label"


def test_two_runs_of_one_experiment_get_two_secrets():
    names = {pod_access.campaign_secret_name(c) for c in (_EARLIER, _LATER)}

    assert len(names) == 2
    assert all(len(n) <= 63 for n in names)
    assert all(n.startswith(pod_access.CAMPAIGN_SECRET_PREFIX) for n in names)


def test_two_runs_of_one_experiment_get_two_labels():
    labels = {_label_safe_campaign(f"{_LONG}-2026-10-04-{t}") for t in ("04111039", "04240269")}

    assert len(labels) == 2
    assert all(len(label) <= 63 for label in labels)


def test_a_short_id_is_used_as_it_is():
    """A name that fits is not rewritten, so existing short campaigns keep their objects."""
    assert _label_safe_campaign("nav-2026-10-04-04111039") == "nav-2026-10-04-04111039"
    assert (pod_access.campaign_secret_name("nav-2026-10-04-04111039")
            == pod_access.CAMPAIGN_SECRET_PREFIX + "nav-2026-10-04-04111039")


def test_a_shortened_name_is_stable_and_a_valid_label():
    """Recomputed wherever the campaign is addressed, so it must come out the same each time,
    and end alphanumeric as a label value and an object name must."""
    first = pod_access.campaign_secret_name(_LATER)

    assert first == pod_access.campaign_secret_name(_LATER)
    assert first[-1].isalnum()
    assert _label_safe_campaign(_LONG + "-x" * 10)[-1].isalnum()
