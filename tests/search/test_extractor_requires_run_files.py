# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""An extractor that cannot find what it reads says so, instead of scoring a constant.

Extraction runs between batches so the strategy can be asked for the next one;
postprocessing runs once, over the finished campaign. So a file a ``postprocessing:``
step writes does not exist when an extractor runs -- on any run, of any cell, ever.

Nothing about that was visible. The ordinary thing for code to do with a path that is not
there is return ``None`` or ``0.0``, and an objective that is the same value everywhere is
a search with no gradient that reports itself converged. It happened twice, in sibling
extractors, and both times it was caught by a human noticing the score never moved.

Two things close it here. A cell with nothing to read is refused for every extractor,
including one from outside this repository. And an extractor may declare the per-run files
it needs, which turns "absent from every run" into a ``NoSampleError`` naming the file.
"""

# Exercises the evaluator against hand-built extractors.
# pylint: disable=import-outside-toplevel

from pathlib import Path

import pytest

from robovast.common.config import SearchConfig
from robovast.search.evaluator import Evaluator
from robovast.search.extractor import Extractor, ExtractResult, NoSampleError
from robovast.search.types import ParamSet


def _cfg(plugin="failure_rate"):
    return SearchConfig(
        strategy="random",
        search_space={"x": {"type": "float", "low": 0, "high": 1}},
        extract={"plugin": plugin},
        objectives=[{"name": "score", "direction": "maximize"}],
        per_batch=1, budget=[{"batches": 1}], seed=1,
    )


def _run(config_dir: Path, run: str, *, files=()):
    run_dir = config_dir / run
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "test.xml").write_text(
        '<testsuite errors="0" failures="0" tests="1"><testcase name="t"/></testsuite>')
    for name in files:
        (run_dir / name).write_text("v\n1.0\n")
    return run_dir


class _Constant(Extractor):
    """The shape of the defect: a missing file becomes a number, quietly."""

    def extract(self, config_dir: Path) -> ExtractResult:
        rows = list(config_dir.glob("*/nav2_behaviors.csv"))
        return ExtractResult(objectives={"score": float(len(rows))})


class _Declaring(_Constant):
    requires_run_files = ("nav2_behaviors.csv",)


def _evaluator(extractor_cls, tmp_path, monkeypatch, plugin="tests:Extractor"):
    monkeypatch.setattr("robovast.search.evaluator.load_ref",
                        lambda ref, group, vast_dir: extractor_cls)
    cfg = _cfg(plugin=plugin)
    return Evaluator(cfg, str(tmp_path))


# -- a cell with nothing to read ---------------------------------------------


def test_a_cell_with_no_runs_at_all_refuses_a_declared_file(tmp_path, monkeypatch):
    """No run has the file, because there is no run. Still the honest answer."""
    config_dir = tmp_path / "camp" / "c1"
    config_dir.mkdir(parents=True)
    evaluator = _evaluator(_Declaring, tmp_path, monkeypatch)

    with pytest.raises(NoSampleError) as excinfo:
        evaluator.evaluate(config_dir, ParamSet(id=1, values={"x": 0.5}))
    assert "nav2_behaviors.csv" in str(excinfo.value)


# -- a declared file that no run has -----------------------------------------


def test_a_declared_file_absent_from_every_run_is_a_refusal(tmp_path, monkeypatch):
    """**The regression.** Undeclared, this cell scores 0.0 and so does every other."""
    config_dir = tmp_path / "camp" / "c1"
    _run(config_dir, "0")
    _run(config_dir, "1")
    evaluator = _evaluator(_Declaring, tmp_path, monkeypatch)

    with pytest.raises(NoSampleError) as excinfo:
        evaluator.evaluate(config_dir, ParamSet(id=1, values={"x": 0.5}))

    message = str(excinfo.value)
    assert "nav2_behaviors.csv" in message
    # The message has to name the reason, not just the absence: the file is missing on
    # purpose at this point in the campaign, and that is what the reader needs told.
    assert "postprocessing" in message


def test_the_same_cell_without_the_declaration_scores_a_constant(tmp_path, monkeypatch):
    """What the declaration buys, stated as the behaviour it replaces.

    Undeclared, the extractor is called, meets no file, and returns 0.0 -- a number
    indistinguishable from a cell that genuinely measured zero.
    """
    config_dir = tmp_path / "camp" / "c1"
    _run(config_dir, "0")
    evaluator = _evaluator(_Constant, tmp_path, monkeypatch)

    result = evaluator.evaluate(config_dir, ParamSet(id=1, values={"x": 0.5}))
    assert result.objectives == {"score": 0.0}


def test_a_declared_file_that_is_there_scores_normally(tmp_path, monkeypatch):
    config_dir = tmp_path / "camp" / "c1"
    _run(config_dir, "0", files=("nav2_behaviors.csv",))
    _run(config_dir, "1", files=("nav2_behaviors.csv",))
    evaluator = _evaluator(_Declaring, tmp_path, monkeypatch)

    result = evaluator.evaluate(config_dir, ParamSet(id=1, values={"x": 0.5}))
    assert result.objectives == {"score": 2.0}
    assert result.n_samples == 2


def test_a_declared_file_missing_from_only_some_runs_is_left_to_the_extractor(
        tmp_path, monkeypatch):
    """One trial's data being odd is not the structural case, and aggregating over runs
    is the extractor's job -- refusing the cell would take a usable sample away."""
    config_dir = tmp_path / "camp" / "c1"
    _run(config_dir, "0", files=("nav2_behaviors.csv",))
    _run(config_dir, "1")
    evaluator = _evaluator(_Declaring, tmp_path, monkeypatch)

    result = evaluator.evaluate(config_dir, ParamSet(id=1, values={"x": 0.5}))
    assert result.objectives == {"score": 1.0}


def test_a_bare_string_declaration_is_read_as_one_name_at_runtime(tmp_path, monkeypatch):
    """``vast config validate`` refuses it, but a campaign that got past that must not
    quietly check for a file called ``n``."""
    class _Stringly(_Constant):
        requires_run_files = "nav2_behaviors.csv"

    config_dir = tmp_path / "camp" / "c1"
    _run(config_dir, "0")
    evaluator = _evaluator(_Stringly, tmp_path, monkeypatch)

    with pytest.raises(NoSampleError) as excinfo:
        evaluator.evaluate(config_dir, ParamSet(id=1, values={"x": 0.5}))
    assert "nav2_behaviors.csv" in str(excinfo.value)


def test_declaring_nothing_changes_nothing(tmp_path, monkeypatch):
    """The default has to be inert: every extractor that predates this keeps working."""
    config_dir = tmp_path / "camp" / "c1"
    _run(config_dir, "0", files=("nav2_behaviors.csv",))
    evaluator = _evaluator(_Constant, tmp_path, monkeypatch)

    assert evaluator.evaluate(
        config_dir, ParamSet(id=1, values={"x": 0.5})).objectives == {"score": 1.0}


# -- the declaration itself is checked --------------------------------------


def _validate(extractor_cls):
    from robovast.common.config_validation import _requires_run_files_problems

    return _requires_run_files_problems(extractor_cls, "./e.py:E")


def test_a_well_formed_declaration_raises_no_problem():
    assert _validate(_Declaring) == []
    assert _validate(_Constant) == []


def test_a_string_declaration_is_refused_with_the_tuple_that_was_meant():
    class _Stringly(_Constant):
        requires_run_files = "poses.csv"

    problems = _validate(_Stringly)
    assert len(problems) == 1
    message = str(problems[0])
    assert "poses.csv" in message and "tuple" in message


def test_a_declaration_that_cannot_be_iterated_is_refused():
    class _Numbered(_Constant):
        requires_run_files = 7

    assert len(_validate(_Numbered)) == 1


def test_an_entry_that_is_not_a_filename_is_refused():
    class _Odd(_Constant):
        requires_run_files = ("poses.csv", None, "")

    assert len(_validate(_Odd)) == 2


def test_a_name_that_leaves_the_run_directory_is_refused():
    class _Escaping(_Constant):
        requires_run_files = ("../../etc/passwd", "/abs/poses.csv")

    problems = _validate(_Escaping)
    assert len(problems) == 2
    assert all("relative to a run directory" in str(p) for p in problems)
