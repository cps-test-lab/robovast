# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Every notebook the Explorer declares must exist in the project being validated.

The failure this prevents is slow and quiet. Staging skips a declared notebook that is not
there with one warning in the controller log, so the campaign runs to completion, reports
``finished``, and the Explorer tab fails only when someone opens the results -- by which time
the log line that said why is thousands of lines back, if it was kept at all.

It is also easy to hit: a project pushed without its ``analysis/`` directory declares five
notebooks and ships none. That is what happened to one campaign in this repo's own dataset,
whose ``_config/`` has no ``analysis/`` and whose controller log carries exactly five of those
warnings.
"""

from robovast.common.config_validation import _explorer_notebook_problems


def _viz(notebooks):
    """A raw config whose Explorer declares *notebooks*."""
    return {"visualization": {"results": {"explorer": {"notebooks": notebooks}}}}


def _analysis(tmp_path, *names):
    (tmp_path / "analysis").mkdir(exist_ok=True)
    for name in names:
        (tmp_path / "analysis" / name).write_text("{}\n")


def test_a_declared_notebook_that_is_absent_is_reported(tmp_path):
    raw = _viz([{"Analysis": {"run": "analysis/analysis_run.ipynb"}}])
    problems = _explorer_notebook_problems(raw, str(tmp_path))
    assert len(problems) == 1
    assert problems[0]["field"] == "visualization.results.explorer.notebooks[0].Analysis.run"
    # The message has to name the file, the scope and the workload: with several workloads
    # declaring several scopes, "not found" alone does not say which declaration is wrong.
    message = problems[0]["message"]
    assert "analysis/analysis_run.ipynb" in message
    assert "run" in message and "Analysis" in message


def test_a_present_notebook_is_accepted(tmp_path):
    _analysis(tmp_path, "analysis_run.ipynb")
    raw = _viz([{"Analysis": {"run": "analysis/analysis_run.ipynb"}}])
    assert _explorer_notebook_problems(raw, str(tmp_path)) == []


def test_every_missing_notebook_is_reported_not_just_the_first(tmp_path):
    # Validation's contract is to report everything at once, before compute is spent. A
    # project pushed without its analysis/ directory is short every notebook, and finding
    # them one re-push at a time is the cost this check exists to avoid.
    raw = _viz([
        {"Analysis": {"run": "analysis/analysis_run.ipynb",
                      "config": "analysis/analysis_config.ipynb",
                      "campaign": "analysis/analysis_campaign.ipynb"}},
        {"Resource Usage": {"run": "analysis/resource_usage_run.ipynb",
                            "config": "analysis/resource_usage_config.ipynb"}},
    ])
    problems = _explorer_notebook_problems(raw, str(tmp_path))
    assert len(problems) == 5
    assert [p["field"] for p in problems] == [
        "visualization.results.explorer.notebooks[0].Analysis.run",
        "visualization.results.explorer.notebooks[0].Analysis.config",
        "visualization.results.explorer.notebooks[0].Analysis.campaign",
        "visualization.results.explorer.notebooks[1].Resource Usage.run",
        "visualization.results.explorer.notebooks[1].Resource Usage.config",
    ]


def test_one_missing_notebook_among_present_ones_is_still_reported(tmp_path):
    # The half-staged case: enough notebooks exist that the Explorer shows the workload, so
    # the tab bar looks right and only one scope is broken.
    _analysis(tmp_path, "analysis_run.ipynb", "analysis_campaign.ipynb")
    raw = _viz([{"Analysis": {"run": "analysis/analysis_run.ipynb",
                              "config": "analysis/analysis_config.ipynb",
                              "campaign": "analysis/analysis_campaign.ipynb"}}])
    problems = _explorer_notebook_problems(raw, str(tmp_path))
    assert len(problems) == 1
    assert "analysis/analysis_config.ipynb" in problems[0]["message"]


def test_a_path_that_escapes_the_project_is_reported(tmp_path):
    # Notebooks are copied into the campaign's _config/ by their relative path, so one that
    # climbs out of the project has no meaning there -- and would be resolved against the
    # service's filesystem rather than the author's.
    raw = _viz([{"Analysis": {"run": "../elsewhere/analysis_run.ipynb"}}])
    problems = _explorer_notebook_problems(raw, str(tmp_path))
    assert len(problems) == 1
    assert problems[0]["stage"] == "notebook"


def test_no_explorer_block_is_not_a_problem(tmp_path):
    # The block is optional; a campaign declaring no notebooks has nothing to check.
    assert _explorer_notebook_problems({}, str(tmp_path)) == []
    assert _explorer_notebook_problems(_viz(None), str(tmp_path)) == []
    assert _explorer_notebook_problems(_viz([]), str(tmp_path)) == []
