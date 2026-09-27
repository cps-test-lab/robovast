# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The scenario-parameter cache is keyed on every file the scenario imports."""

import os
import time

import pytest

from robovast.common.common import get_scenario_parameters, scenario_inputs


@pytest.fixture(name="parses")
def _parses(monkeypatch):
    """Count the parses scenario-execution is asked for."""
    calls = []

    def parse(scenario_file):
        calls.append(scenario_file)
        return {"nav": [{"name": f"p{len(calls)}", "type": "string", "is_list": False}]}

    monkeypatch.setattr("scenario_execution.get_scenario_parameters", parse)
    return calls


def _project(tmp_path):
    lib = tmp_path / "lib" / "nav.osc"
    lib.parent.mkdir()
    lib.write_text("scenario nav:\n    goal: string\n")
    main = tmp_path / "main.osc"
    main.write_text(f'import osc.helpers\nimport "{lib}"\n')
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

    get_scenario_parameters(str(main))
    assert len(parses) == 2


def test_the_inputs_follow_file_and_library_imports(tmp_path):
    main, lib = _project(tmp_path)
    inputs = scenario_inputs(str(main))
    assert inputs[:1] == [str(main)]
    assert str(lib) in inputs
    assert any(p.endswith(os.path.join("lib_osc", "helpers.osc")) for p in inputs)
    # helpers.osc imports osc.types in turn.
    assert any(p.endswith(os.path.join("lib_osc", "types.osc")) for p in inputs)


def test_a_missing_import_is_refused(tmp_path):
    main = tmp_path / "main.osc"
    main.write_text(f'import "{tmp_path / "gone.osc"}"\n')
    with pytest.raises(FileNotFoundError, match="gone.osc"):
        scenario_inputs(str(main))
