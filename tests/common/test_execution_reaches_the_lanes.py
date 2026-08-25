# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""What a ``.vast`` declares under ``execution:``, and what the lanes are actually handed.

The unit tests pin each field's *consumer*; this pins the seam before them. Both lanes read
``campaign_data["execution"]`` and nothing else, so a key that composition does not carry is
a key the run behaves as if nobody declared -- silently, and identically to a typo.

Every existing test of these fields builds the lane's ``execution`` dict by hand, which is
why four fields could be dropped here for as long as they were: the backends were covered,
the seam feeding them was not.
"""

import textwrap

import yaml

from robovast.common.config import ExecutionConfig
from robovast.common.config_generation import generate_scenario_variations

_SCENARIO = """\
import osc.robotics

scenario nav:
    do serial:
        wait elapsed(1s)
"""

#: One declared value per ``ExecutionConfig`` field a lane has to see, each chosen to differ
#: from that field's default -- a field that silently fell back to its default would
#: otherwise pass by coincidence.
#:
#: Adding a field to the model without adding it here (or to
#: ``COMPOSITION_ONLY_EXECUTION_KEYS``) fails
#: :func:`test_every_execution_field_is_carried_or_says_why`, which is the point: the choice
#: of whether the lanes can see a field should be made when the field is added, not
#: discovered later by a campaign that quietly did nothing.
_DECLARED = {
    "env": [{"SEAM": "carried"}],
    "runs": 3,
    "timeout": 321,
    "simulation": "some.module:Class",
    "mode": "ros2",
    "runs_per_job": 2,
    "shm_size": "256Mi",
}

#: Carried, but not an ``ExecutionConfig`` field -- ``containers`` is rewritten by
#: ``apply_backend`` before it is handed on, so it is asserted structurally below rather
#: than compared to a literal.
_CONTAINERS = ("sut", "scenario")


def _project(tmp_path):
    """A ``.vast`` declaring every field in :data:`_DECLARED`, rendered from that mapping.

    Rendered rather than written out, so the file and the expectations cannot drift: a
    field added to ``_DECLARED`` is declared by the project automatically.
    """
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    declared = yaml.safe_dump(_DECLARED, default_flow_style=False, sort_keys=True)
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent("""\
        version: 3
        metadata: {name: seam}
        configuration:
        - name: base
        execution:
          containers:
            sut: {image: sut:latest}
            scenario: {image: scen:latest}
          scenario_file: scenario.osc
        """) + textwrap.indent(declared, "  "))
    return vast


def _compose(vast, tmp_path):
    return generate_scenario_variations(
        str(vast), progress_update_callback=lambda m: None,
        output_dir=str(tmp_path / "gen"), use_cache=False)


def test_every_declared_execution_key_survives_composition(tmp_path):
    """The regression this file exists for.

    ``shm_size`` and ``timeout`` were declared by real campaigns, documented, and validated
    -- and neither reached a lane, because the assembly in ``config_generation`` listed the
    keys to carry by hand and these were not among them.
    """
    execution = _compose(_project(tmp_path), tmp_path)["execution"]

    missing = {k: v for k, v in _DECLARED.items() if execution.get(k) != v}
    assert not missing, (
        f"composition dropped {sorted(missing)}; both lanes read campaign_data['execution'] "
        f"and nothing else, so these declarations never take effect")
    assert set(execution.get("containers") or {}) >= set(_CONTAINERS)


def test_every_execution_field_is_carried_or_says_why(tmp_path):
    """The systemic guard, so the *next* field cannot be lost the way these four were.

    Asserted against the model rather than a hand-written list: the two drifting apart is
    precisely the failure being prevented, so a second list would reintroduce it.
    """
    from robovast.common.config_generation import COMPOSITION_ONLY_EXECUTION_KEYS

    unexplained = (set(ExecutionConfig.model_fields)
                   - set(_DECLARED) - set(COMPOSITION_ONLY_EXECUTION_KEYS)
                   - {"containers"})
    assert not unexplained, (
        f"{sorted(unexplained)} are ExecutionConfig fields this test says nothing about. "
        f"Either give the field a distinguishable value in _DECLARED, so the seam is proven "
        f"to carry it, or name it in COMPOSITION_ONLY_EXECUTION_KEYS to say the lanes are "
        f"not meant to see it.")

    # And the exception list is exactly that -- an exception. A key named there but still
    # handed to the lanes would make the list a comment rather than a rule.
    execution = _compose(_project(tmp_path), tmp_path)["execution"]
    leaked = set(execution) & set(COMPOSITION_ONLY_EXECUTION_KEYS)
    assert not leaked, f"{sorted(leaked)} is consumed by composition but handed on anyway"
