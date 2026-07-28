# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``cluster setup`` reads node labels without requiring a project.

Setup deploys into a cluster and runs from any directory, so a ``.robovast_project``
must not be a precondition — while a ``.vast`` that *is* named (``-V``, or the project's
when run inside one) must still be honoured, and an unreadable one must fail loudly
rather than deploy the cluster with no node selectors.
"""

import json

import click
import pytest

from robovast.execution.cluster_execution.cluster_setup import \
    get_kubernetes_node_labels_from_config

_VAST = """version: 1
execution:
  image: i
  runs: 1
  kubernetes:
    jobs:
      node_labels:
        node-pool: primary
    control:
      node_labels:
        node-pool: extra
"""


def _write_vast(path):
    path.write_text(_VAST, encoding="utf-8")
    return path


def _with_vast_file_override(path):
    """Call the reader inside a Click context carrying ``--vast-file`` = *path*."""
    result = {}

    @click.command()
    @click.pass_context
    def _cmd(ctx):
        ctx.ensure_object(dict)
        ctx.obj['vast_file'] = str(path)
        result['labels'] = get_kubernetes_node_labels_from_config()

    _cmd.main([], standalone_mode=False)
    return result['labels']


def test_no_project_and_no_vast_file_yields_no_labels(tmp_path, monkeypatch):
    """Project-free setup is legal: no config found means no node selectors."""
    monkeypatch.chdir(tmp_path)
    assert get_kubernetes_node_labels_from_config() == (None, None)


def test_vast_file_override_supplies_labels(tmp_path, monkeypatch):
    """``vast -V <file>`` is the project-free way to name the labels' source."""
    monkeypatch.chdir(tmp_path)
    vast = _write_vast(tmp_path / "campaign.vast")
    assert _with_vast_file_override(vast) == ({'node-pool': 'primary'},
                                             {'node-pool': 'extra'})


def test_project_config_still_supplies_labels(tmp_path, monkeypatch):
    """Run inside a project and its ``.vast`` is used, as before."""
    _write_vast(tmp_path / "campaign.vast")
    (tmp_path / ".robovast_project").write_text(json.dumps(
        {"config": "campaign.vast", "results_dir": "results"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert get_kubernetes_node_labels_from_config() == ({'node-pool': 'primary'},
                                                        {'node-pool': 'extra'})


def test_unreadable_named_config_raises(tmp_path, monkeypatch):
    """A named config that cannot be read must abort setup, not mean "no labels"."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="could not read node labels"):
        get_kubernetes_node_labels_from_config(str(tmp_path / "missing.vast"))
