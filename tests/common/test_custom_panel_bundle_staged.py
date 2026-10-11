# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A custom run-view panel's built bundle is collected from where the schema declares it."""

from robovast.common.config_generation import _collect_analysis_input_files


def test_custom_run_view_panel_bundle_is_collected(tmp_path):
    bundle = tmp_path / "panels" / "mine"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "remoteEntry.js").write_text("// entry")
    (bundle / "assets" / "chunk.js").write_text("// chunk")
    parameters = {"visualization": {"results": {"run_view": {"panels": [
        "playback", {"custom": {"remote": "panels/mine"}}]}}}}

    collected = _collect_analysis_input_files(parameters, base_dir=str(tmp_path))

    assert sorted(collected) == ["panels/mine/assets/chunk.js", "panels/mine/remoteEntry.js"]
