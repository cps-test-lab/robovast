# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A campaign id must fit in the bucket name the cluster lane's embedded object store
derives it into -- refused here, at mint time, rather than left to surface as a
storage-layer error once a campaign has already been accepted and started.
"""

import pytest

from robovast.common.errors import CampaignConfigError
from robovast.execution import controller


def test_a_long_name_is_refused_before_any_id_is_minted():
    with pytest.raises(CampaignConfigError) as excinfo:
        controller.campaign_id_for(None, name_override="a" * 50)

    message = str(excinfo.value)
    assert "50" in message, "must name the slug's own length"
    assert str(controller._MAX_CAMPAIGN_ID_LEN) in message  # noqa: SLF001
    assert "shorten" in message


def test_a_name_at_the_boundary_is_accepted():
    # 43 chars is the documented budget once the fixed timestamp suffix is subtracted.
    cid = controller.campaign_id_for(None, name_override="a" * 43)
    assert len(cid) <= controller._MAX_CAMPAIGN_ID_LEN  # noqa: SLF001


def test_an_ordinary_name_is_unaffected():
    cid = controller.campaign_id_for(None, name_override="nav2-baseline")
    assert cid.startswith("nav2-baseline-")
    assert len(cid) <= controller._MAX_CAMPAIGN_ID_LEN  # noqa: SLF001
