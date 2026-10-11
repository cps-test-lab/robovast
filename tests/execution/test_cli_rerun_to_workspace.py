# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast campaign rerun --to-workspace`` names a next step a client install can run.

The work order is a workspace on the service, so the check that follows the edits is the
service's own validation of that workspace, not a verb that reads a file on this disk.
"""

import contextlib

from click.testing import CliRunner

from robovast.client import campaign_cli
from robovast.service.interface import MigrationMarker, WorkOrder


class _Service:
    def materialize_retrigger_workspace(self, campaign_id, workspace_name):
        return WorkOrder(workspace_id="ws-ab12", config_path="nav/nav.vast", reached=3,
                         markers=[MigrationMarker(path="execution.image", reason="pick one")])


def test_the_next_step_validates_the_workspace_on_the_service(monkeypatch):
    @contextlib.contextmanager
    def _service(*_a, **_k):
        yield _Service(), "fake service"

    monkeypatch.setattr(campaign_cli, "service_client", _service)
    result = CliRunner().invoke(campaign_cli.campaign,
                                ["rerun", "nav-2026-01-01-000000", "--to-workspace", "nav"])

    assert result.exit_code == 0, result.output
    assert "vast workspace validate ws-ab12 nav/nav.vast" in result.output
    assert "vast configuration validate" not in result.output
