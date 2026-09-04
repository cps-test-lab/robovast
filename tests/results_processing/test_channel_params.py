# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A factor is a ``param_*`` column whichever channel it was written on.

``params_json`` carries the scenario channel only -- on a search campaign it is the parameter
set the strategy proposed, which the replay feeds back, so it cannot carry anything else. The
``sim`` and ``sut`` channels reach the index through ``unit.channels_json`` instead, and these
tests pin how a destination becomes a column: named from the END of the path, extended
leftwards only as far as uniqueness demands, and never silently shared between two
destinations.
"""

import sqlite3

import pytest

from robovast.common.campaign_data import read_config_channels
from robovast.results_processing.campaign_ingest import (_channel_params, _flatten_channel,
                                                         _read_units)


def _sim(friction, radius, retries=2):
    return {
        "sim": {"world": "depot.yaml",
                "components": {"floorplan": {"floor": {"friction": friction}}}},
        "sut": {"nav2.local_costmap.local_costmap.ros__parameters.inflation_layer."
                "inflation_radius": radius,
                "bt.//RecoveryNode[@name='NavigateRecovery']/@number_of_retries": retries},
    }


def test_a_destination_is_named_by_the_end_of_its_path():
    """The whole destination does not fit an identifier; its tail is what a reader types."""
    params = _channel_params({"a": _sim(0.6, 0.30), "b": _sim(1.4, 0.55)})

    assert params["a"]["sut_inflation_radius"] == 0.30
    assert params["b"]["sut_inflation_radius"] == 0.55
    assert params["a"]["sim_friction"] == 0.6
    # An XPath destination is not identifier-shaped anywhere but its end, which is exactly
    # the part that names the factor.
    assert params["a"]["sut_number_of_retries"] == 2


def test_two_destinations_sharing_a_leaf_do_not_share_a_column():
    """The one failure this must not have: one column holding two different factors."""
    channels = {
        "a": {"sim": {"components": {"floor": {"friction": 0.6},
                                     "wall": {"friction": 1.0}}}},
        "b": {"sim": {"components": {"floor": {"friction": 1.4},
                                     "wall": {"friction": 2.0}}}},
    }
    params = _channel_params(channels)

    assert params["a"] == {"sim_floor_friction": 0.6, "sim_wall_friction": 1.0}
    assert params["b"] == {"sim_floor_friction": 1.4, "sim_wall_friction": 2.0}


def test_uniqueness_is_decided_across_the_campaign_not_within_one_configuration():
    """The runs table is one shape for every row, so per-cell uniqueness is not enough."""
    channels = {
        "a": {"sim": {"components": {"floor": {"friction": 0.6}}}},
        "b": {"sim": {"components": {"wall": {"friction": 1.0}}}},
    }
    params = _channel_params(channels)

    # Each configuration alone would have been content with `sim_friction`.
    assert list(params["a"]) == ["sim_floor_friction"]
    assert list(params["b"]) == ["sim_wall_friction"]


def test_the_channel_is_always_in_the_name():
    """So a sim: and a sut: destination ending in the same word stay two columns -- and
    neither can be mistaken for the scenario parameter of that name."""
    channels = {"a": {"sim": {"timeout": 30}, "sut": {"nav2.timeout": 5}}}
    params = _channel_params(channels)

    assert params["a"] == {"sim_timeout": 30, "sut_timeout": 5}


def test_a_destination_too_long_to_name_is_reported_not_dropped_silently(caplog):
    """A column silently missing is the same wrong answer as a column silently shared.

    Length only bites once ambiguity has driven the name back to the whole destination: two
    paths identical but for their source, which no shorter suffix can tell apart.
    """
    tail = ".".join(f"segment_number_{n}" for n in range(8))
    params = _channel_params({"a": {"sut": {f"first.{tail}": 1, f"second.{tail}": 2}}})

    assert params["a"] == {}
    assert "channels_json" in caplog.text


def test_a_flattened_block_reads_as_the_vast_wrote_it():
    """``sut`` blocks arrive flat and must pass through untouched; ``sim`` blocks nest."""
    assert _flatten_channel({"a": {"b": 1}, "c": 2}) == {"a.b": 1, "c": 2}
    assert _flatten_channel({"nav2.x.y": 1}) == {"nav2.x.y": 1}
    # An empty mapping is a leaf: there is nothing below it to name.
    assert _flatten_channel({"a": {}}) == {"a": {}}


def _store(path, rows, *, with_channels=True):
    """A ``campaign.db`` with just the ``unit`` columns the ingest reads."""
    con = sqlite3.connect(path)
    columns = ("config_name TEXT, params_json TEXT, objective REAL, status TEXT, "
               "paramset_id TEXT" + (", channels_json TEXT" if with_channels else ""))
    con.execute(f"CREATE TABLE unit (id INTEGER PRIMARY KEY, {columns})")
    for idx, row in enumerate(rows, 1):
        placeholders = ", ".join("?" * (len(row) + 1))
        con.execute(f"INSERT INTO unit VALUES ({placeholders})", (idx, *row))
    con.commit()
    con.close()


def test_a_store_predating_the_column_still_yields_its_params(tmp_path):
    """The retry exists so an older campaign keeps the params it does have."""
    path = tmp_path / "campaign.db"
    _store(path, [("cfg-1", '{"speed": 1.0}', 0.5, "evaluated", "cfg-1")],
           with_channels=False)

    params, channels, _objectives, failed = _read_units(path)

    assert params == {"cfg-1": {"speed": 1.0}}
    assert channels == {"cfg-1": {}}
    assert failed == []


def test_channels_are_read_beside_params_not_merged_into_them(tmp_path):
    """A search resume must keep seeing only what the strategy proposed."""
    path = tmp_path / "campaign.db"
    _store(path, [("cfg-1", '{"speed": 1.0}', 0.5, "evaluated", "cfg-1",
                   '{"sim": {"friction": 0.6}}')])

    params, channels, _objectives, _failed = _read_units(path)

    assert params == {"cfg-1": {"speed": 1.0}}
    assert channels == {"cfg-1": {"sim": {"friction": 0.6}}}


def _config_dir(tmp_path, **files):
    config = tmp_path / "cfg-1" / "_config"
    config.mkdir(parents=True)
    for name, text in files.items():
        (config / f"{name}.config").write_text(text, encoding="utf-8")
    return tmp_path / "cfg-1"


def test_the_index_reads_the_same_records_the_results_tree_keeps(tmp_path):
    """One reader for all three, so what reaches the index is what the campaign recorded."""
    config_dir = _config_dir(
        tmp_path,
        scenario="my_scenario:\n  goal_pose: {x: 1.0}\n",
        sim="world: depot.yaml\ncomponents:\n  floor:\n    friction: 0.6\n",
        sut="nav2.inflation_radius: 0.3\n")

    channels = read_config_channels(config_dir)

    assert channels == {
        "scenario": {"goal_pose": {"x": 1.0}},
        "sim": {"world": "depot.yaml", "components": {"floor": {"friction": 0.6}}},
        "sut": {"nav2.inflation_radius": 0.3},
    }


@pytest.mark.parametrize("present", [("scenario",), ("scenario", "sim"), ()])
def test_a_channel_the_campaign_does_not_use_has_no_key(tmp_path, present):
    """The result says which channels a configuration actually has, rather than inventing
    empty ones a reader would have to tell apart from a channel that resolved to nothing."""
    files = {"scenario": "s:\n  a: 1\n", "sim": "world: x\n", "sut": "a.b: 1\n"}
    config_dir = _config_dir(tmp_path, **{k: files[k] for k in present})

    assert set(read_config_channels(config_dir)) == set(present)
