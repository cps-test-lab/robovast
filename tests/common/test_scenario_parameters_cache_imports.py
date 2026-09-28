# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The scenario-parameter cache is keyed on every file the parse read."""

import os
import time

import pytest

from robovast.common.common import get_scenario_parameters


@pytest.fixture(name="parses")
def _parses(monkeypatch):
    """Count the parses scenario-execution is asked for; each reads the scenario and lib/nav.osc."""
    calls = []

    def parse(scenario_file):
        calls.append(scenario_file)
        lib = os.path.join(os.path.dirname(scenario_file), "lib", "nav.osc")
        return {"nav": [{"name": f"p{len(calls)}", "type": "string", "is_list": False}]}, [scenario_file, lib]

    monkeypatch.setattr("scenario_execution.get_scenario_parameters_and_inputs", parse, raising=False)
    return calls


def _project(tmp_path):
    lib = tmp_path / "lib" / "nav.osc"
    lib.parent.mkdir()
    lib.write_text("scenario nav:\n    goal: string\n")
    main = tmp_path / "main.osc"
    main.write_text('import "lib/nav.osc"\n')
    return main, lib


def test_an_unchanged_scenario_is_served_from_the_cache(tmp_path, parses):
    main, _ = _project(tmp_path)
    first = get_scenario_parameters(str(main))
    assert get_scenario_parameters(str(main)) == first
    assert len(parses) == 1


def test_a_changed_import_is_not_served_from_the_cache(tmp_path, parses):
    main, lib = _project(tmp_path)
    get_scenario_parameters(str(main))

    lib.write_text("scenario nav:\n    goal: string\n    speed: float\n")
    later = time.time() + 5
    os.utime(lib, (later, later))

    assert get_scenario_parameters(str(main))["nav"][0]["name"] == "p2"
    assert len(parses) == 2
