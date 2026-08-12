# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A ``batch`` notebook is addressable over the web, like the desktop viewer's.

The batch is the unit of a search's history, but it is a *logical* level: a search
campaign's configurations sit flat under the campaign root whichever round proposed them,
so there is no batch directory to hand a notebook as ``DATA_DIR``. The contract is
therefore two halves, and a notebook is only correct if both hold:

* ``DATA_DIR`` is the campaign root — the same directory a ``campaign`` notebook gets, and
* the round is passed separately, injected as ``BATCH``.

Pinned here because dropping the injection does not fail: the notebook would render
happily against the whole campaign and report one round's results as another's.
"""

from pathlib import Path

import pytest

from robovast.service.client import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

_VAST = """\
configuration:
  name: x
evaluation:
  visualization:
    - Analysis:
        campaign: analysis/campaign.ipynb
        batch: analysis/batch.ipynb
        config: analysis/config.ipynb
        run: analysis/run.ipynb
"""


@pytest.fixture
def transport(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "robovast.common.cli.project_config.ProjectConfig.load",
        staticmethod(lambda *a, **k: None))
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return LocalTransport(store=store)


@pytest.fixture
def campaign(transport):
    """A campaign directory with a snapshot ``.vast`` declaring all four notebook levels."""
    cid = "search-1"
    config_dir = Path(transport._data_dir(cid)) / "_config"  # noqa: SLF001
    (config_dir / "analysis").mkdir(parents=True)
    (config_dir / "sim.vast").write_text(_VAST, encoding="utf-8")
    for level in ("campaign", "batch", "config", "run"):
        (config_dir / "analysis" / f"{level}.ipynb").write_text("{}", encoding="utf-8")
    return cid


def test_batch_is_offered_as_a_notebook_level(transport, campaign):
    """The Explorer builds its tab strip from this, so an omitted level is an absent tab."""
    workloads = transport.list_campaign_visualizations(campaign).workloads
    assert [w.name for w in workloads] == ["Analysis"]
    assert set(workloads[0].levels) == {"run", "config", "batch", "campaign"}


def test_a_batch_node_gets_the_campaign_root_as_its_data_dir(transport, campaign):
    """A batch has no directory of its own; it borrows the campaign's."""
    base = transport._node_data_dir(campaign, "campaign", "", None)  # noqa: SLF001
    assert transport._node_data_dir(campaign, "batch", "", None) == base  # noqa: SLF001
    # And the levels that *do* have one still resolve to it.
    assert transport._node_data_dir(campaign, "config", "cfg-a", None) == str(  # noqa: SLF001
        Path(base) / "cfg-a")


def test_the_round_is_injected_as_batch(transport, campaign, monkeypatch):
    """What distinguishes one round's notebook from another's, since DATA_DIR cannot."""
    seen = {}

    def fake_render(notebook_path, data_dir, **kwargs):
        seen.update(notebook_path=notebook_path, data_dir=data_dir, **kwargs)
        return "<html></html>"

    monkeypatch.setattr("robovast.results_processing.notebook_render.render_notebook_html",
                        fake_render)

    transport.render_campaign_notebook(campaign, "Analysis", "batch", batch=3)
    assert seen["inject"] == {"BATCH": 3}
    assert seen["notebook_path"].endswith("analysis/batch.ipynb")
    assert seen["data_dir"] == transport._node_data_dir(  # noqa: SLF001
        campaign, "campaign", "", None)


def test_a_level_without_a_batch_injects_nothing(transport, campaign, monkeypatch):
    """So a notebook's own ``BATCH = None`` default survives and it can say it was not told."""
    seen = {}
    monkeypatch.setattr(
        "robovast.results_processing.notebook_render.render_notebook_html",
        lambda notebook_path, data_dir, **kwargs: seen.update(kwargs) or "<html></html>")

    transport.render_campaign_notebook(campaign, "Analysis", "config", config_name="cfg-a")
    assert seen["inject"] is None
