# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The generated entrypoint records the node's instance type.

``get_instance_type_command`` is part of the cluster-provider contract (an abstract method
on ``BaseConfig``) and every provider implements it, but nothing called it: the entrypoint
hardcoded ``INSTANCE_TYPE=""``, so the recorded instance type was empty on every run,
cloud included. These tests pin the wiring, and that a provider which cannot answer leaves
the field empty rather than failing a campaign over sysinfo collection.
"""

import tempfile
from pathlib import Path

import pytest

from robovast.common.execution import prepare_campaign_configs
from robovast.execution.cluster_execution.cluster_setup import get_cluster_config
from robovast.execution.cluster_execution.kubernetes_backend import \
    _instance_type_command


@pytest.fixture
def campaign_data(tmp_path):
    """The minimum a campaign needs staged: a .vast and a scenario file that exist."""
    (tmp_path / "s.vast").write_text("version: 1\n", encoding="utf-8")
    (tmp_path / "s.osc").write_text("scenario x:\n    do serial:\n        wait elapsed(1s)\n",
                                    encoding="utf-8")
    return {"vast": str(tmp_path / "s.vast"), "scenario_file": str(tmp_path / "s.osc"),
            "configs": [{"name": "c1", "config": {}}], "execution": {"runs": 1}}


def _instance_type_line(campaign_data, **kwargs) -> str:
    """The generated entrypoint's INSTANCE_TYPE assignment."""
    out = tempfile.mkdtemp()
    prepare_campaign_configs(out, dict(campaign_data), **kwargs)
    script = Path(out, "_transient", "entrypoint.sh").read_text(encoding="utf-8")
    assert "@@INSTANCE_TYPE_BLOCK@@" not in script, \
        "the placeholder was left in the generated entrypoint"
    return next(line.strip() for line in script.splitlines()
                if "INSTANCE_TYPE=" in line and "collect_sysinfo" not in line)


@pytest.mark.parametrize("provider", ["rke2", "minikube", "gcp", "azure"])
def test_provider_command_reaches_the_entrypoint(campaign_data, provider):
    """Each installed cluster config's command is what the entrypoint runs."""
    config = get_cluster_config(provider)
    command = _instance_type_command(config)
    assert command, f"{provider} implements get_instance_type_command"
    assert _instance_type_line(campaign_data, cluster=True,
                               instance_type_command=command) == command


def test_local_lane_records_no_instance_type(campaign_data):
    """A local Docker run is not an instance of anything; empty ingests as NULL."""
    assert _instance_type_line(campaign_data, cluster=False) == 'INSTANCE_TYPE=""'


def test_provider_without_the_hook_degrades_to_empty(campaign_data):
    """``BaseConfig`` raises NotImplementedError. Sysinfo collection is explicitly
    non-fatal, so a provider that cannot answer must not fail the campaign."""
    class _NoImpl:
        def get_instance_type_command(self):
            raise NotImplementedError

    assert _instance_type_command(_NoImpl()) is None
    assert _instance_type_line(campaign_data, cluster=True,
                              instance_type_command=None) == 'INSTANCE_TYPE=""'


def test_every_installed_provider_implements_the_hook():
    """The hook is abstract on BaseConfig, so a provider missing it records nothing —
    which is silent. Catch it here instead."""
    missing = [name for name in ("rke2", "minikube", "gcp", "azure")
               if _instance_type_command(get_cluster_config(name)) is None]
    assert not missing, f"cluster configs without get_instance_type_command: {missing}"
