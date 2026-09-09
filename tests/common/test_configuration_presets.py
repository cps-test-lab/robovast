# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``configuration_presets:`` — what ``use:`` composes, in what order, and what is refused."""

import logging

import pytest

from robovast.common.config_presets import expand_configuration_presets as expand


def _cfg(presets, *entries):
    return {"version": 4,
            "execution": {"containers": {"scenario": {"image": "a"}}, "runs": 1},
            "configuration_presets": presets,
            "configuration": list(entries)}


def _entry(config):
    return expand(config)["configuration"][0]


def _params(config, channel):
    return (_entry(config).get("parameters") or {}).get(channel) or {}


# -- opting out --------------------------------------------------------------------

def test_a_config_with_no_presets_and_no_use_is_returned_untouched():
    config = {"version": 4, "configuration": [{"name": "a"}]}
    assert expand(config) is config


# -- the case YAML merge keys get silently wrong -----------------------------------

def test_every_preset_contributing_to_one_subtree_survives():
    """``<<: [*a, *b, *c]`` keeps only the first contributor to each key, with no error.

    That is the whole reason this exists, so it is the first thing asserted.
    """
    config = _cfg(
        {"robot": {"parameters": {"scenario": {"description_package": "tb4"},
                                  "sim": {"config": "world/nav2.yaml"}}},
         "stack": {"parameters": {"scenario": {"params_file": "nav2.yaml"},
                                  "sut": {"bringup.bt.xml": "/config/bt.xml"}}},
         "trial": {"parameters": {"scenario": {"map_file": "hexagon.yaml"},
                                  "sim": {"overrides": {"seed": 3}}}}},
        {"name": "cell", "use": ["robot", "stack", "trial"]})
    assert _params(config, "scenario") == {"description_package": "tb4",
                                           "params_file": "nav2.yaml",
                                           "map_file": "hexagon.yaml"}
    assert _params(config, "sim") == {"config": "world/nav2.yaml", "overrides": {"seed": 3}}
    assert _params(config, "sut") == {"bringup.bt.xml": "/config/bt.xml"}


def test_the_sim_channel_comes_back_nested():
    """A configuration carries one shape for this channel whether presets were involved."""
    config = _cfg({"p": {"parameters": {"sim": {"overrides": {"a": {"b": 1}}}}}},
                  {"name": "c", "use": ["p"]})
    assert _params(config, "sim") == {"overrides": {"a": {"b": 1}}}


def test_the_presets_block_is_consumed():
    config = _cfg({"p": {"parameters": {"scenario": {"x": 1}}}}, {"name": "c", "use": ["p"]})
    out = expand(config)
    assert "configuration_presets" not in out
    assert "use" not in out["configuration"][0]


# -- the precedence chain, one test per adjacent pair -------------------------------

def test_the_entrys_parameters_beat_a_presets():
    config = _cfg({"p": {"parameters": {"scenario": {"goal": 1}, "sut": {"a.b": 1}}}},
                  {"name": "c", "use": ["p"],
                   "parameters": {"scenario": {"goal": 2}, "sut": {"a.b": 2}}})
    assert _params(config, "scenario")["goal"] == 2
    assert _params(config, "sut")["a.b"] == 2


def test_a_presets_variation_beats_the_entrys_fixed_value():
    """The surprising layer, and the one that keeps the existing rule intact: a variation
    has always won over the fixed value it varies."""
    config = _cfg({"p": {"variations": [{"ParameterVariationList":
                                         {"sut": "a.b", "values": [1, 2]}}]}},
                  {"name": "c", "use": ["p"], "parameters": {"sut": {"a.b": 9}}})
    entry = _entry(config)
    assert entry["parameters"]["sut"] == {"a.b": 9}
    assert entry["variations"][0]["ParameterVariationList"]["values"] == [1, 2]


def test_the_entrys_variation_replaces_a_presets_on_the_same_destination():
    """Not crossed with: sweeping one destination twice makes cells that differ in nothing."""
    config = _cfg({"p": {"variations": [{"ParameterVariationList":
                                         {"sut": "a.b", "values": [1, 2]}}]}},
                  {"name": "c", "use": ["p"],
                   "variations": [{"ParameterVariationList":
                                   {"sut": "a.b", "values": [7]}}]})
    variations = _entry(config)["variations"]
    assert len(variations) == 1
    assert variations[0]["ParameterVariationList"]["values"] == [7]


def test_an_entry_variation_on_another_destination_is_an_extra_axis():
    config = _cfg({"p": {"variations": [{"ParameterVariationList":
                                         {"sut": "a.b", "values": [1, 2]}}]}},
                  {"name": "c", "use": ["p"],
                   "variations": [{"ParameterVariationList":
                                   {"sut": "c.d", "values": [7]}}]})
    assert len(_entry(config)["variations"]) == 2


def test_presets_apply_in_use_order():
    config = _cfg({"a": {"parameters": {"scenario": {"x": 1}}},
                   "b": {"parameters": {"scenario": {"y": 2}}}},
                  {"name": "c", "use": ["a", "b"]})
    assert _params(config, "scenario") == {"x": 1, "y": 2}


def test_absence_works_as_an_override():
    config = _cfg({"p": {"parameters": {"sut": {"a.b": 1}}}},
                  {"name": "c", "use": ["p"], "parameters": {"sut": {"a.b": {"$absent": True}}}})
    assert _params(config, "sut")["a.b"] == {"$absent": True}


# -- refusals -----------------------------------------------------------------------

def test_an_undefined_preset_is_refused_listing_the_defined_ones():
    config = _cfg({"robot-tb4": {}}, {"name": "c", "use": ["nope"]})
    with pytest.raises(ValueError, match="robot-tb4"):
        expand(config)


def test_an_undefined_preset_with_no_presets_at_all_says_so():
    config = {"version": 4, "configuration": [{"name": "c", "use": ["nope"]}]}
    with pytest.raises(ValueError, match=r"\(none defined\)"):
        expand(config)


def test_two_presets_writing_one_destination_are_refused_naming_both():
    config = _cfg({"a": {"parameters": {"sut": {"nav2.x": 1}}},
                   "b": {"parameters": {"sut": {"nav2.x": 2}}}},
                  {"name": "c", "use": ["a", "b"]})
    with pytest.raises(ValueError) as exc:
        expand(config)
    assert "'a'" in str(exc.value) and "'b'" in str(exc.value) and "nav2.x" in str(exc.value)


def test_the_sim_collision_check_sees_through_the_two_spellings():
    """One preset writes it nested, the other dotted; they are the same destination."""
    config = _cfg({"a": {"parameters": {"sim": {"overrides": {"seed": 1}}}},
                   "b": {"parameters": {"sim": {"overrides.seed": 2}}}},
                  {"name": "c", "use": ["a", "b"]})
    with pytest.raises(ValueError, match="overrides.seed"):
        expand(config)


def test_two_presets_sweeping_one_destination_are_refused():
    config = _cfg({"a": {"variations": [{"V": {"sut": "a.b", "values": [1]}}]},
                   "b": {"variations": [{"V": {"sut": "a.b", "values": [2]}}]}},
                  {"name": "c", "use": ["a", "b"]})
    with pytest.raises(ValueError, match="both vary"):
        expand(config)


def test_a_preset_named_twice_in_one_use_is_refused():
    config = _cfg({"a": {}}, {"name": "c", "use": ["a", "a"]})
    with pytest.raises(ValueError, match="twice"):
        expand(config)


def test_an_entry_overriding_a_preset_is_not_a_collision():
    """The layer above is allowed to win, silently; only preset-vs-preset is ambiguous."""
    config = _cfg({"a": {"parameters": {"sut": {"nav2.x": 1}}}},
                  {"name": "c", "use": ["a"], "parameters": {"sut": {"nav2.x": 2}}})
    assert _params(config, "sut")["nav2.x"] == 2


def test_an_unused_preset_warns_and_does_not_refuse(caplog):
    """Refusing would break ``export-configs``, which copies every preset and uses a subset."""
    config = _cfg({"used": {"parameters": {"scenario": {"x": 1}}}, "spare": {}},
                  {"name": "c", "use": ["used"]})
    with caplog.at_level(logging.WARNING):
        expand(config)
    assert "spare" in caplog.text


# -- purity -------------------------------------------------------------------------

def test_expansion_is_idempotent_and_does_not_mutate_its_input():
    config = _cfg({"p": {"parameters": {"scenario": {"x": 1}}}}, {"name": "c", "use": ["p"]})
    import copy
    before = copy.deepcopy(config)
    once = expand(config)
    assert config == before
    assert expand(once) == once


def test_neither_expander_imports_the_config_models():
    """Same rule a migration step follows: they run before the models exist."""
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "robovast" / "common"
    for name in ("config_presets.py", "config_extends.py", "config_channels.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "robovast.common.config", name
                assert node.module != "config", name
