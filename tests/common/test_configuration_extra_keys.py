# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A ``configuration`` entry refuses keys it does not declare — except when reading an archive.

The cost of the old permissive default was not a confusing file: a dropped key gives a
campaign that runs its full sweep against an unconfigured baseline and reports every cell
normally, so nothing downstream says the file did not mean what it reads.
"""

import logging

import pytest

from robovast.common.config import validate_config


def _cfg(*entries):
    return {"version": 4,
            "execution": {"containers": {"scenario": {"image": "a"}}, "runs": 1},
            "configuration": list(entries)}


# -- authoring: strict -------------------------------------------------------------

def test_a_misspelled_key_is_refused_rather_than_dropped():
    with pytest.raises(ValueError, match="configuration.0.paramaters"):
        validate_config(_cfg({"name": "a", "paramaters": {"scenario": {"goal": 1}}}))


def test_the_refusal_names_every_offending_entry():
    with pytest.raises(ValueError) as exc:
        validate_config(_cfg({"name": "a", "wrold": 1}, {"name": "b", "sut2": {}}))
    assert "configuration.0.wrold" in str(exc.value)
    assert "configuration.1.sut2" in str(exc.value)


def test_a_sibling_sut_is_now_refused_rather_than_ignored():
    """The v3 spelling. Left ignored it would read as set and be written nowhere."""
    with pytest.raises(ValueError, match="configuration.0.sut"):
        validate_config(_cfg({"name": "a", "sut": {"nav2.a": 1}}))


def test_the_declared_keys_are_all_still_accepted():
    c = validate_config(_cfg({"name": "a",
                              "parameters": {"scenario": {"goal": 1},
                                             "sim": {"overrides": {"x": 1}},
                                             "sut": {"nav2.a.b": 2}},
                              "variations": []}))
    assert c.configuration[0].parameters.sut == {"nav2.a.b": 2}


# -- archive reads: lenient --------------------------------------------------------

def test_an_archived_campaign_with_a_stray_key_still_reads(caplog):
    """It already ran, with the key ignored. Refusing it now loses the results and
    catches nothing — so drop it, and say which key and which configuration."""
    with caplog.at_level(logging.WARNING):
        c = validate_config(_cfg({"name": "a", "uses": ["x"],
                                  "parameters": {"sut": {"nav2.a": 1}}}), strict=False)
    assert c.configuration[0].parameters.sut == {"nav2.a": 1}
    assert "uses" in caplog.text and "'a'" in caplog.text


def test_the_lenient_path_still_enforces_everything_else():
    """Leniency is scoped to unknown keys; a bad value is still a bad value."""
    with pytest.raises(ValueError, match="name"):
        validate_config(_cfg({"name": "Not Lowercase", "stray": 1}), strict=False)


def test_a_clean_config_is_untouched_by_the_lenient_path():
    entry = {"name": "a", "parameters": {"sut": {"nav2.a": 1}}}
    c = validate_config(_cfg(dict(entry)), strict=False)
    assert c.configuration[0].model_dump(exclude_none=True) == {
        "name": "a", "parameters": {"sut": {"nav2.a": 1}}}
