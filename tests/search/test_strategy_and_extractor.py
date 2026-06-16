# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""RandomSearch, the failure_rate extractor, the codec, and load_ref."""

import numpy as np

from robovast.common.config import SearchConfig
from robovast.common.plugin_ref import load_ref
from robovast.search.compose import apply_override, config_name_for
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


def _cfg(strategy="random", direction="maximize", generations=2, per_step=8, space=None):
    return SearchConfig(
        strategy=strategy,
        search_space=space or SPACE,
        extract={"plugin": "failure_rate"},
        objectives=[{"name": "fr", "direction": direction}],
        per_step=per_step, budget={"generations": generations}, seed=0,
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


def test_random_budget_and_ranking_maximize():
    s = build_strategy(_cfg(generations=2))
    assert not s.is_done()
    g = s.ask(3)
    s.tell([Evaluation(params=g[0], objectives={"fr": 0.2}),
            Evaluation(params=g[1], objectives={"fr": 0.9}),
            Evaluation(params=g[2], objectives={"fr": 0.5})])
    s.tell([])
    assert s.is_done()
    assert s.report().best.objectives["fr"] == 0.9


def test_direction_minimize_flips_best():
    s = build_strategy(_cfg(direction="minimize", generations=1))
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
    assert res.measures == {}


def test_failure_rate_missing_dir_is_zero(tmp_path):
    assert FailureRate().extract(tmp_path / "nope").objectives == {"failure_rate": 0.0}


# ---- load_ref ----

def test_load_ref_entry_point():
    assert load_ref("failure_rate", EXTRACTOR_GROUP).__name__ == "FailureRate"


def test_load_ref_file(tmp_path):
    mod = tmp_path / "myext.py"
    mod.write_text("class MyThing:\n    value = 42\n")
    cls = load_ref("myext.py:MyThing", EXTRACTOR_GROUP, str(tmp_path))
    assert cls.value == 42


# ---- compose helpers ----

def test_apply_override_routes():
    block = {"name": "base", "variations": [{"Path": {"seed": 41}}]}
    apply_override(block, "variations.Path.seed", 7)
    apply_override(block, "noise", 0.3)
    assert block["variations"][0]["Path"]["seed"] == 7
    assert {"noise": 0.3} in block["parameters"]


def test_config_name_schema_valid():
    name = config_name_for(ParamSet(values={"a": 1}))
    assert name.islower() and "_" not in name and "." not in name
