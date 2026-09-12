# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""When a campaign stops reading the project it was launched from.

That moment is what a bulk push into a workspace waits for, so it has to be a fact about
the campaign rather than a guess. A batch campaign reaches it as soon as it has staged. A
search campaign never does: it composes again every generation.
"""

import textwrap
import types

from robovast.execution.backends import RunOptions
from robovast.execution.control_server import ControllerState


def _controller(tmp_path, mode="batch", **kw):
    from robovast.common.store import STORE_FILENAME, CampaignStore
    from robovast.execution.controller import CampaignController

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "sweep.vast").write_text(textwrap.dedent("""\
        version: 4
        execution:
          containers: {scenario: {image: ghcr.io/cps-test-lab/robovast:latest}}
          runs: 1
          scenario_file: room.osc
        """), encoding="utf-8")
    (project / "room.osc").write_text("scenario x\n", encoding="utf-8")
    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    if mode == "search":
        kw.setdefault("strategy", object())
    return CampaignController(
        campaign_id="camp", results_dir=str(tmp_path), runs=1, backend=None,
        options=RunOptions(), store=store, campaign_config_dump={"version": 1},
        vast_dir=str(project), state=ControllerState(campaign_id="camp"),
        notifier=types.SimpleNamespace(
            start_heartbeat=lambda **k: None, started=lambda mode: None,
            batch_finished=lambda *a: None, campaign_finished=lambda *a, **k: None),
        **kw)


def test_staging_is_what_wires_the_backends_to_the_controller(tmp_path):
    """The lanes stage inside their own run_batch, so the controller cannot watch for it.

    Set on the options the controller drives rather than asked of whoever built them: a
    caller that forgot would leave the campaign holding its project for its whole life,
    silently.
    """
    controller = _controller(tmp_path)
    assert controller.options.on_configs_staged == controller._on_configs_staged


def test_a_batch_campaign_releases_its_project_once_it_has_staged(tmp_path):
    """It composed once and staged once; everything after that is its own directory.

    So the project it came from can change without changing it -- which is what lets an
    author push to that workspace while their runs are still going, instead of waiting
    out a sweep the push could not have affected anyway.
    """
    controller = _controller(tmp_path)

    controller._on_configs_staged()

    assert controller.state.project_released


def test_a_search_campaign_keeps_reading_the_project_it_was_launched_from(tmp_path):
    """A search composes again every generation, so staging settles nothing for it.

    Releasing here would let a push change an experiment already under way: generation two
    would compose from files generation one never saw, and two generations that read
    different files are not one experiment. A search earns the same freedom only by
    composing from its own copy of the project, which this does not give it.
    """
    controller = _controller(tmp_path, mode="search")

    controller._on_configs_staged()

    assert not controller.state.project_released


def test_a_campaign_releases_its_project_once_and_not_per_batch(tmp_path):
    """Staging happens per batch; the release is a one-way fact about the campaign."""
    controller = _controller(tmp_path)
    controller._on_configs_staged()

    controller._on_configs_staged()

    assert controller.state.project_released
