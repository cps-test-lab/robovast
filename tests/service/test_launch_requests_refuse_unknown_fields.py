# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A launch request refuses a field it does not declare, rather than ignoring it."""

import pytest
from pydantic import ValidationError

from robovast.service.interface import CreateCampaignRequest, ExecRequest


@pytest.mark.parametrize("model,required", [
    (CreateCampaignRequest, {"workspace_id": "ws-1"}),
    (ExecRequest, {}),
])
def test_an_unknown_field_is_refused_by_name(model, required):
    with pytest.raises(ValidationError, match="show_gui"):
        model(**required, show_gui=True)
