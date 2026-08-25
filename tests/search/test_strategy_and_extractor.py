# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""RandomSearch, the failure_rate extractor, the codec, and load_ref."""

import numpy as np
import pytest

from robovast.common.config import SearchConfig
from robovast.common.plugin_ref import load_ref
from robovast.search.compose import _substitute_vars, config_name_for
from robovast.search.extractors.failure_rate import FailureRate
from robovast.search.plugins import EXTRACTOR_GROUP
from robovast.search.space import SearchSpaceCodec
from robovast.search.strategy import build_strategy
from robovast.search.types import Evaluation, ParamSet

SPACE = {
    "thrust": {"type": "float", "low": 0.3, "high": 3.0},
    "mass": {"type": "int", "low": 1, "high": 5},
    "mode": {"type": "choice", "values": ["a", "b", "c"]},
}


def _cfg(strategy="random", direction="maximize", batches=2, per_batch=8, space=None):
    return SearchConfig(
        strategy=strategy,
        search_space=space or SPACE,
        extract={"plugin": "failure_rate"},
        objectives=[{"name": "fr", "direction": direction}],
        per_batch=per_batch, budget=[{"batches": batches}], seed=0,
    )


def _evs(param_sets, value_fn):
    return [Evaluation(params=p, objectives={"fr": value_fn(p)}) for p in param_sets]


# ---- RandomSearch ----

def test_random_samples_within_domains():
    s = build_strategy(_cfg())
    for p in s.ask(50):
        assert 0.3 <= p.values["thrust"] <= 3.0
        assert 1 <= p.values["mass"] <= 5 and isinstance(p.values["mass"], int)
        assert p.values["mode"] in ("a", "b", "c")


def test_random_reproducible_with_seed():
    a = [p.values for p in build_strategy(_cfg()).ask(20)]
    b = [p.values for p in build_strategy(_cfg()).ask(20)]
    assert a == b


def test_random_ranking_maximize():
    # Batches/budget are enforced by the controller now, not the strategy; here we
    # only check that report() ranks by the objective.
    s = build_strategy(_cfg(batches=2))
    g = s.ask(3)
    s.tell([Evaluation(params=g[0], objectives={"fr": 0.2}),
            Evaluation(params=g[1], objectives={"fr": 0.9}),
            Evaluation(params=g[2], objectives={"fr": 0.5})])
    assert s.report().best.objectives["fr"] == 0.9


def test_direction_minimize_flips_best():
    s = build_strategy(_cfg(direction="minimize", batches=1))
    g = s.ask(3)
    s.tell([Evaluation(params=g[0], objectives={"fr": 0.2}),
            Evaluation(params=g[1], objectives={"fr": 0.9}),
            Evaluation(params=g[2], objectives={"fr": 0.5})])
    # minimize -> lowest objective is best
    assert s.report().best.objectives["fr"] == 0.2


# ---- codec ----

def test_codec_roundtrip_and_bounds():
    codec = SearchSpaceCodec(_cfg().search_space)   # typed (validated) dims
    lo, hi = codec.bounds()
    assert codec.dim == 3
    assert np.allclose(lo, 0.0) and np.allclose(hi, 1.0)
    values = {"thrust": 1.5, "mass": 3, "mode": "b"}
    decoded = codec.decode(codec.encode(values))
    assert abs(decoded["thrust"] - 1.5) < 1e-6
    assert decoded["mass"] == 3 and decoded["mode"] == "b"
    # arbitrary in-cube vectors decode in-bounds
    for vec in np.random.RandomState(0).rand(20, 3):
        d = codec.decode(vec)
        assert 0.3 <= d["thrust"] <= 3.0 and 1 <= d["mass"] <= 5 and d["mode"] in ("a", "b", "c")


# ---- failure_rate extractor ----

def test_failure_rate_aggregates_runs(tmp_path):
    config_dir = tmp_path / "cfg"
    for run, failures in [("0", 1), ("1", 1), ("2", 0)]:
        d = config_dir / run
        d.mkdir(parents=True)
        (d / "test.xml").write_text(
            f'<testsuite errors="0" failures="{failures}" tests="1">'
            f'<testcase name="t" time="1.0"/></testsuite>')
    res = FailureRate().extract(config_dir)
    assert res.objectives == {"failure_rate": 2 / 3}
    assert not res.measures


def test_failure_rate_refuses_to_score_a_cell_that_produced_nothing(tmp_path):
    """No results at all is NOT failure_rate 0.0.

    This asserted 0.0 until 2026-08-21, and 0.0 is a fabricated observation: it claims
    nothing failed, when in fact nothing ran, and the two are then indistinguishable.
    It is worse for a *maximized* objective -- 0.0 is the least interesting score, so the
    search steers away from exactly the parameter sets whose runs are dying.

    The old behaviour was the only safe one at the time: raising would have aborted the
    campaign. Now the controller records a NoSampleError cell and carries on (see
    test_a_sample_less_cell_is_recorded_and_skipped_not_fatal), so refusing to invent a
    number costs nothing -- which is what makes the honest answer available.
    """
    from robovast.search.extractor import NoSampleError

    with pytest.raises(NoSampleError):
        FailureRate().extract(tmp_path / "nope")

    # A directory that exists but holds no test.xml is the same case, not a different one.
    empty = tmp_path / "ran-but-recorded-nothing"
    (empty / "0").mkdir(parents=True)
    with pytest.raises(NoSampleError):
        FailureRate().extract(empty)


# ---- load_ref ----

def test_load_ref_entry_point():
    assert load_ref("failure_rate", EXTRACTOR_GROUP).__name__ == "FailureRate"


def test_load_ref_file(tmp_path):
    mod = tmp_path / "myext.py"
    mod.write_text("class MyThing:\n    value = 42\n")
    cls = load_ref("myext.py:MyThing", EXTRACTOR_GROUP, str(tmp_path))
    assert cls.value == 42


# ---- compose helpers ----

def test_substitute_vars_typed_and_marker_forms():
    template = [{"Path": {"seed": "$seed", "length": "${path_len}",
                          "map": "office.yaml", "start": "@start_pose"}}]
    used = set()
    out = _substitute_vars(template, {"seed": 7, "path_len": 12.5}, used)
    path = out[0]["Path"]
    assert path["seed"] == 7 and isinstance(path["seed"], int)       # typed, $name
    assert path["length"] == 12.5                                    # ${name}
    assert path["map"] == "office.yaml"                              # plain string
    assert path["start"] == "@start_pose"                            # @ ref untouched
    assert used == {"seed", "path_len"}


def test_substitute_vars_escape_and_unknown():
    used = set()
    assert _substitute_vars("$$literal", {}, used) == "$literal"     # $$ escape
    try:
        _substitute_vars("$missing", {"seed": 1}, set())
    except ValueError as e:
        assert "search_space variable" in str(e)
    else:
        raise AssertionError("expected ValueError for unknown marker")


def test_config_name_schema_valid():
    name = config_name_for(ParamSet(values={"a": 1}))
    assert name.islower() and "_" not in name and "." not in name


def test_an_empty_builtin_group_is_reported_as_a_broken_install(tmp_path, monkeypatch):
    """``(none registered)`` for a group robovast declares is not a missing plugin.

    It is the signature of a shadowed installation: ``importlib.metadata`` deduplicates
    distributions by name and keeps the first on ``sys.path``, so a second ``robovast``
    ahead of the real one replaces its entry points wholesale -- every group at once. A
    workspace plugin directory carrying its own copy did that to a whole service, and the
    old message answered with advice for a typo ("run 'poetry install'").
    """
    import sys

    from robovast.common import plugin_ref
    from robovast.common.config_plugins import PLUGIN_DIRNAME

    # A workspace plugin directory holding its own robovast, as the outage produced.
    site = tmp_path / PLUGIN_DIRNAME / "venv" / "lib" / "python3.12" / "site-packages"
    di = site / "robovast-1.0.0.dist-info"
    di.mkdir(parents=True)
    (di / "METADATA").write_text("Metadata-Version: 2.1\nName: robovast\nVersion: 1.0.0\n")
    (di / "entry_points.txt").write_text("[robovast.variation_types]\nOne = x:Y\n")
    monkeypatch.syspath_prepend(str(site))

    with pytest.raises(ValueError) as excinfo:
        plugin_ref.load_ref("optuna", "robovast.search_strategies")

    message = str(excinfo.value)
    assert "(none registered)" in message
    assert "broken installation" in message
    assert str(site) in message                  # names the directory to remove
    assert "restart" in message                  # and why removing files is not enough
    assert "poetry install" not in message       # the advice that sent readers astray
    assert str(site) in sys.path                 # the diagnosis changed no state


def test_an_ordinary_unknown_name_is_still_an_ordinary_error():
    """The diagnosis must not fire when the group is healthy and the name is simply wrong."""
    from robovast.common import plugin_ref

    with pytest.raises(ValueError) as excinfo:
        plugin_ref.load_ref("no-such-strategy", "robovast.search_strategies")

    message = str(excinfo.value)
    assert "optuna" in message                   # lists what IS available
    assert "broken installation" not in message
