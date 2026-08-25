# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A container a launch could not find an image for must be refused before the launch.

The schema deliberately lets a known role omit ``image``, and that is right for two of them:
``scenario`` falls back to the RoboVAST framework image, and ``simulation`` gets one from its
backend. But the fallback in ``_resolve_image`` is for the **main container only** -- on purpose,
because inventing an image for the system under test would run something nobody named.

So a ``sut`` with no image validated cleanly and then died at launch with "no image for container
'sut'". Both halves were defensible; the disagreement was not. Validation now applies the launch's
own rule, which is the same reason the image-provenance tiers share one classifier.
"""

import pytest
import yaml

from robovast.common.containers import containers_without_a_resolvable_image, plan_containers


def _execution(**containers):
    return {"mode": "ros2", "containers": containers}


def test_the_main_container_may_omit_its_image():
    """The normal case, not an omission: it means the framework image, whose project and tag are
    the deployment's to choose."""
    assert containers_without_a_resolvable_image(_execution(scenario={})) == []


def test_a_non_main_container_with_no_image_is_reported():
    names = [n for n, _ in containers_without_a_resolvable_image(
        _execution(scenario={}, sut={"resources": {"cpu": 1}}))]
    assert names == ["sut"]


def test_a_container_robovast_builds_is_fine():
    """The build supplies the ref, so an author who declares packages never needs an image. This
    is why basic_nav_roqsim.vast validates with its sut image deleted -- it builds that container.
    """
    for key in ("system_packages", "python_packages"):
        assert containers_without_a_resolvable_image(
            _execution(scenario={}, sut={key: ["something"]})) == []


def test_a_declared_image_is_enough():
    assert containers_without_a_resolvable_image(
        _execution(scenario={}, sut={"image": "reg/x:1"})) == []


def test_the_message_names_both_ways_out():
    _name, why = containers_without_a_resolvable_image(
        _execution(scenario={}, sut={}))[0]
    assert "execution.containers.sut.image" in why
    assert "system_packages" in why
    # And says why there is no fallback, so the refusal reads as a decision rather than a gap.
    assert "nobody named" in why


def test_validation_and_launch_now_agree():
    """The point of the change. Before it, exactly this config passed validation and then raised
    from _resolve_image -- so the same config had two answers depending on how far it got."""
    execution = _execution(scenario={}, sut={"resources": {"cpu": 1}})
    assert containers_without_a_resolvable_image(execution), "validation should object"
    with pytest.raises(ValueError, match="no image for container 'sut'"):
        plan_containers(execution, main_image_fallback="ghcr.io/x/robovast:latest")


def test_the_collect_all_validator_reports_it(tmp_path):
    from robovast.common.config_validation import validate_project_file

    vast = tmp_path / "c.vast"
    vast.write_text(yaml.safe_dump({
        "version": 2, "metadata": {"name": "p"},
        "execution": {"mode": "ros2",
                      "containers": {"scenario": {}, "sut": {"resources": {"cpu": 1}}},
                      "scenario_file": "s.osc", "runs": 1}}), encoding="utf-8")
    (tmp_path / "s.osc").write_text("scenario p:\n    do serial:\n        wait elapsed(1s)\n",
                                    encoding="utf-8")
    report = validate_project_file(str(vast))
    offending = [p for p in report["problems"] if p["stage"] == "image"]
    assert offending and offending[0]["config"] == "sut"


def test_a_malformed_execution_block_is_left_to_other_checks(tmp_path):
    """This check must not be the one that reports a broken execution section -- it would mask the
    schema error that actually explains the file."""
    from robovast.common.config_validation import _unresolvable_image_problems

    assert _unresolvable_image_problems({"execution": {"containers": "not a mapping"}}) == []
