# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Each campaign's token Secret has a name no other campaign's can have.

The Secret is created idempotently -- an existing one is taken to hold this campaign's
token -- so two campaigns mapping to one name would share the first one's token, and
deleting either campaign would delete the other's.
"""

from robovast.execution.cluster_execution.pod_access import campaign_secret_name


def test_two_campaigns_of_one_long_name_do_not_share_a_token_secret():
    name = "nav2-warehouse-obstacle-avoidance-sweep"
    first = f"{name}-2026-09-26-10000000"
    second = f"{name}-2026-09-26-15303099"

    assert campaign_secret_name(first) != campaign_secret_name(second)


def test_the_longest_campaign_id_still_gives_a_valid_secret_name():
    """A Secret is named by a DNS subdomain: at most 253 characters, lower-case."""
    campaign_id = "x" * 43 + "-2026-09-26-10000000"  # 63, the mint-time maximum
    secret = campaign_secret_name(campaign_id)

    assert secret.endswith("-2026-09-26-10000000")
    assert len(secret) <= 253 and secret == secret.lower()
