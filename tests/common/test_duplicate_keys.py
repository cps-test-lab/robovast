# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A key written twice in a ``.vast`` is refused, not resolved.

The failure this closes is not a confusing file. YAML keeps the last of two keys, so the
losing block stays in the file, still reads as the configuration, and is applied to nothing --
and the campaign runs, reports every cell normally, and was configured by something nobody
wrote. It is reachable by ordinary means: a block indented at a list's level attaches to the
entry *above* it, so a section written in the wrong order overwrites the entry before it.
"""

import logging

import pytest

from robovast.common.common import load_config
from robovast.common.config_validation import _safe_load
from robovast.common.yaml_strict import DuplicateKeyError, load, load_all

CAMPAIGN = """\
version: 4
execution:
  containers: {scenario: {image: a}}
  runs: 1
"""


def _write(tmp_path, text, name="campaign.vast"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# -- what is refused ---------------------------------------------------------------

def test_a_repeated_key_is_refused_naming_both_lines():
    with pytest.raises(DuplicateKeyError) as exc:
        load("cfg:\n  sut: {a: 1}\n  other: 2\n  sut: {b: 2}\n")
    message = str(exc.value)
    assert "'sut'" in message
    assert "line 2" in message and "line 4" in message


def test_the_block_that_lands_on_the_entry_above_is_caught(tmp_path):
    """The shape that motivated this: a `sut:` block written after one entry's body and
    before the next entry's `- name:` belongs, to YAML, to the entry above."""
    path = _write(tmp_path, CAMPAIGN + """\
configuration:
- name: first
  parameters:
    sut: {nav2.global.filters: [keepout]}

# --- the next section -----------------------------------------------------
  parameters:
    sut: {nav2.global.filters: [speed]}
- name: second
""")
    with pytest.raises(DuplicateKeyError, match="'parameters'"):
        load_config(str(path))


def test_the_refusal_says_what_the_file_was_doing():
    """Naming the key is not enough -- the author has to learn that one block was inert."""
    with pytest.raises(DuplicateKeyError) as exc:
        load("a:\n  k: 1\n  k: 2\n")
    assert "applied to nothing" in str(exc.value)


def test_the_file_is_named(tmp_path):
    path = _write(tmp_path, "a:\n  k: 1\n  k: 2\n")
    with pytest.raises(DuplicateKeyError, match="campaign.vast"):
        load_config(str(path))


# -- what is not refused -----------------------------------------------------------

def test_anchors_and_merge_keys_still_work():
    """`<<:` merges a mapping in; the merged keys are not repeats of each other."""
    doc = load("base: &b {x: 1, y: 2}\nderived:\n  <<: *b\n  y: 3\n")
    assert doc["derived"] == {"x": 1, "y": 3}


def test_the_same_key_in_sibling_mappings_is_not_a_repeat():
    doc = load("configuration:\n- name: a\n  parameters: {scenario: {g: 1}}\n"
               "- name: b\n  parameters: {scenario: {g: 2}}\n")
    assert [c["name"] for c in doc["configuration"]] == ["a", "b"]


def test_every_shipped_vast_is_accepted():
    """The corpus is the regression net: a check this strict has to admit real campaigns."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    checked = 0
    for vast in sorted(root.rglob("*.vast")):
        load_all(vast.read_text(encoding="utf-8"), path=str(vast))
        checked += 1
    assert checked > 20, f"expected the shipped corpus to be checked, saw {checked}"


# -- reading a campaign that already ran -------------------------------------------

def test_an_archived_campaign_still_reads_and_says_which_value_ran(tmp_path, caplog):
    """Refusing here would lose the results without catching anything: the campaign ran,
    and it ran with the value a repeat keeps."""
    path = _write(tmp_path, CAMPAIGN + """\
configuration:
- name: a
  parameters:
    sut: {nav2.x: 1}
  parameters:
    sut: {nav2.y: 2}
""")
    with caplog.at_level(logging.WARNING):
        config = load_config(str(path), upgrade=True)
    assert config["configuration"][0]["parameters"] == {"sut": {"nav2.y": 2}}
    assert "'parameters'" in caplog.text


# -- the collect-all validator reports rather than raises --------------------------

def test_the_validator_reports_it_as_a_problem(tmp_path):
    path = _write(tmp_path, "a:\n  k: 1\n  k: 2\n")
    raw, problem = _safe_load(str(path))
    assert raw is None
    assert problem["stage"] == "parse"
    assert "'k'" in problem["message"]
