# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Campaign discovery works without a ``.robovast_project``.

``list_campaigns`` is the only read tool with no absolute-path escape, so it is the
one place where "no project initialized" used to be a dead end. There is no
service-side project any more — a campaign runs a workspace's ``.vast`` — so the
local results root is resolved by precedence (a CWD project's ``results_dir``, else
the dir beside the workspaces store) and an absent root is simply empty.
"""

import pytest

from robovast.mcp_server import results_resolver


@pytest.fixture
def no_project(monkeypatch, tmp_path):
    """No ``.robovast_project`` anywhere; workspaces store rooted under tmp_path."""
    monkeypatch.setattr(
        "robovast.common.cli.project_config.ProjectConfig.load",
        staticmethod(lambda *a, **k: None))
    monkeypatch.setenv("ROBOVAST_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    return tmp_path / "results"


def test_list_campaigns_without_project_file(no_project):
    """A campaign in the fallback root is found with no project file at all."""
    (no_project / "camp-2026-01-01-000000").mkdir(parents=True)
    (no_project / "not-a-campaign").mkdir()

    found = [p.name for p in results_resolver.list_campaigns()]
    assert found == ["camp-2026-01-01-000000"]


def test_list_campaigns_empty_when_root_absent(no_project):
    """A fresh service has no results dir yet: that is empty, not an error."""
    assert not no_project.exists()
    assert results_resolver.list_campaigns() == []


def test_resolve_results_dir_still_raises_for_a_named_campaign(no_project):
    """Naming a campaign under a non-existent root is an error, not silence.

    The asymmetry is deliberate: listing tolerates an absent root, but a caller that
    named something it expects to find must be told the root does not exist -- and
    the message must not say "run 'vast init'", which no longer binds anything.
    """
    with pytest.raises(ValueError, match="No local results directory"):
        results_resolver.resolve_results_dir()


def test_absolute_campaign_path_needs_no_root(no_project, tmp_path):
    """The absolute-path escape keeps working for an archived tree."""
    archived = tmp_path / "elsewhere" / "camp-2026-01-01-000000"
    archived.mkdir(parents=True)
    assert results_resolver.resolve_campaign_path(str(archived)) == archived
