# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Config 4 -> 5: what postprocessing said about building tables, now that the decoder does."""

import pytest

from robovast.common.migrations.config import MIGRATION_MARKER, UnmigratableConfig
from robovast.common.migrations.config.v4_to_v5 import migrate


def test_the_conversion_pods_sizing_is_removed():
    out = migrate({"version": 4, "results_processing": {
        "resources": {"cpu": 8, "memory": "16Gi"}, "postprocessing": ["command"]}})
    assert out["version"] == 5
    assert "resources" not in out["results_processing"]
    assert out["results_processing"]["postprocessing"] == ["command"]


def test_a_bare_entry_for_a_table_built_for_every_run_is_removed():
    out = migrate({"version": 4,
                   "results_processing": {"postprocessing": ["run_log", {"resource_usage": None},
                                                             {"rosbags_tf_to_csv": {
                                                                 "frames": "all"}}]},
                   "search": {"postprocessing": ["run_log", "./m.py:Metrics"]}})
    assert out["results_processing"]["postprocessing"] == [
        {"rosbags_tf_to_csv": {"frames": "all"}}]
    assert out["search"]["postprocessing"] == ["./m.py:Metrics"]


def test_a_severity_filter_at_write_time_is_refused_with_a_marker():
    with pytest.raises(UnmigratableConfig) as refusal:
        migrate({"version": 4, "results_processing": {
            "postprocessing": [{"run_log": {"min_severity": "warn"}}]}})
    assert refusal.value.capability == "run_log.min_severity"
    (marker,) = refusal.value.partial["results_processing"]["postprocessing"]
    assert marker[MIGRATION_MARKER]["was"] == {"run_log": {"min_severity": "warn"}}


def test_the_local_docker_overrides_are_removed():
    out = migrate({"version": 4, "execution": {
        "runs": 2, "local": {"parameter_overrides": {"headless": False}}}})
    assert out["execution"] == {"runs": 2}


def test_the_input_is_not_mutated():
    raw = {"version": 4, "results_processing": {"resources": {"cpu": 1}}}
    migrate(raw)
    assert raw == {"version": 4, "results_processing": {"resources": {"cpu": 1}}}
