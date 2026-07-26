# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Plugin installation is its own campaign phase, logged into the results dir."""

import types

from robovast.execution import controller
from robovast.common import config_plugins
from robovast.common.status import Phase


class _State:
    def __init__(self):
        self.phase = None

    def set_phase(self, phase, stage=None):
        self.phase = phase


def _campaign_config(plugins):
    return types.SimpleNamespace(plugins=plugins)


def test_no_plugins_is_a_noop(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(config_plugins, "ensure_workspace_plugins",
                        lambda *a, **k: called.append((a, k)))
    state = _State()
    controller._install_plugins(str(tmp_path / "x.vast"), _campaign_config(None),
                                str(tmp_path / "camp"), state)
    assert state.phase is None                 # phase unchanged
    assert called == []                        # no install attempted
    assert not (tmp_path / "camp" / "_execution" / "plugin_install.log").exists()


def test_plugins_phase_installs_and_logs(tmp_path, monkeypatch):
    seen = {}

    def fake_ensure(vast_dir, specs, force=False, add_to_path=True):
        seen.update(vast_dir=vast_dir, specs=list(specs), add_to_path=add_to_path)

    monkeypatch.setattr(config_plugins, "ensure_workspace_plugins", fake_ensure)
    vast = tmp_path / "x.vast"
    vast.write_text("version: 1\n")
    campaign_root = tmp_path / "camp"
    state = _State()

    controller._install_plugins(str(vast), _campaign_config(["foo==1"]),
                                str(campaign_root), state)

    assert state.phase == Phase.PLUGIN_INSTALL
    assert seen["specs"] == ["foo==1"]
    assert seen["vast_dir"] == str(tmp_path)   # dir of the .vast
    # Materialize-only: the driver installs but does not import onto its own sys.path.
    assert seen["add_to_path"] is False
    # The install log lands in the campaign results dir like the other phase files.
    assert (campaign_root / "_execution" / "plugin_install.log").is_file()
