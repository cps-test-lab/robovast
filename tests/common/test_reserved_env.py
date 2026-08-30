# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The environment a campaign may not write, and why the list cannot go stale.

``RESERVED_ENV_NAMES`` is a denylist, and a hand-maintained denylist fails in one
direction only: it keeps passing while it protects less and less. Someone adds a variable
to a lane, nobody remembers this list, and a campaign can quietly repoint it.

So the list is checked against the code that actually injects. These tests read the
emitters, extract every environment name they set, and fail when one is neither reserved
nor deliberately left free. That is what makes the list a description of the system rather
than a memory of it.
"""

import re
from pathlib import Path

import pytest

from robovast.common.config import RESERVED_ENV_NAMES, ExecutionConfig
from robovast.common.execution import get_execution_env_variables

_ROOT = Path(__file__).resolve().parents[2] / "src"

#: Names a lane sets that a campaign may legitimately set too. Every one of these steers
#: how a container *renders*, not whether its results mean what they say -- and a campaign
#: running headless, or on a machine whose display is not :0, has a real reason to.
#: Listed explicitly so leaving a name unreserved is a decision someone made, not an
#: omission nobody noticed.
FREE_TO_OVERRIDE = frozenset({
    "DISPLAY", "LIBGL_ALWAYS_SOFTWARE", "QT_X11_NO_MITSHM",
    "NVIDIA_VISIBLE_DEVICES", "NVIDIA_DRIVER_CAPABILITIES",
})

#: (file, pattern) for each place a run's environment is built **on the host** -- the
#: boundary at which a campaign's ``env`` is applied, and therefore the only place an
#: override could take effect. What a container's own entrypoint sets for itself afterwards
#: is out of scope: a campaign cannot reach it, so reserving it would refuse for nothing.
#:
#: The patterns match the *spelling* each emitter uses, so a new variable is caught wherever
#: it is added.
_EMITTERS = (
    ("robovast/execution/execution_utils/execute_local.py",
     r'- ([A-Z][A-Z0-9_]{2,})='),                       # compose `environment:` lines
    ("robovast_cluster/robovast/execution/cluster_execution/kubernetes_backend.py",
     r"'name': '([A-Z][A-Z0-9_]{2,})'"),                # container env entries
    ("robovast_cluster/robovast/execution/cluster_execution/kubernetes_backend.py",
     r"\('(S3_[A-Z_]+)',"),                             # the upload credentials
    ("robovast/common/execution.py",
     r"env\['([A-Z][A-Z0-9_]{2,})'\]"),                 # scenario_env
)


def _base_env_names() -> set:
    """What :func:`get_execution_env_variables` sets, by calling it.

    Read from the function rather than matched in its source: it builds a dict literal,
    and a regex loose enough to catch that would catch every other mapping in the file.
    """
    return set(get_execution_env_variables(0, "")) 


def _injected() -> set:
    """Every environment name the lanes set, read out of the emitters themselves."""
    names = _base_env_names()
    for rel, pattern in _EMITTERS:
        path = _ROOT / rel
        assert path.is_file(), f"emitter moved: {rel}"
        names |= set(re.findall(pattern, path.read_text(encoding="utf-8")))
    return names


def test_every_injected_variable_is_reserved_or_deliberately_free():
    """The drift check.

    If this fails, a lane gained a variable and this list did not. Decide which it is:
    part of the run's protocol (add it to ``RESERVED_ENV_NAMES``) or a rendering hint a
    campaign may steer (add it to ``FREE_TO_OVERRIDE``, with the reason). What must not
    happen is neither, which is how the guard silently stops covering the run.
    """
    unaccounted = _injected() - RESERVED_ENV_NAMES - FREE_TO_OVERRIDE
    assert not unaccounted, (
        "these variables are injected by a lane but are neither reserved nor listed as "
        f"free to override: {', '.join(sorted(unaccounted))}")


def test_the_reserved_list_is_not_carrying_names_nothing_sets():
    """The other direction. A name no emitter sets is either a typo or a leftover, and
    both refuse campaigns for no reason."""
    stale = RESERVED_ENV_NAMES - _injected()
    assert not stale, (
        "reserved but injected by nothing -- remove, or fix the spelling: "
        f"{', '.join(sorted(stale))}")


def test_the_variables_whose_override_would_misreport_a_run_are_covered():
    """The ones worth naming in a test of their own, because the damage is silent.

    Each of these produces a run that *works* and reports something untrue: results
    attributed to a configuration whose parameters were never read, written somewhere the
    campaign will not look, or uploaded with someone else's credentials.
    """
    for name in ("SCENARIO_PARAMETER_FILE", "SCENARIO_FILE", "OUTPUT_DIR",
                 "SCENARIO_OUTPUT_DIR", "RUN_OUTPUT_DIR", "CAMPAIGN_ID",
                 "S3_ACCESS_KEY", "S3_SECRET_KEY"):
        assert name in RESERVED_ENV_NAMES, f"{name} must not be overridable"


@pytest.mark.parametrize("name", sorted(RESERVED_ENV_NAMES))
def test_a_campaign_setting_a_reserved_name_is_refused(name):
    with pytest.raises(ValueError, match="reserved"):
        ExecutionConfig(containers={"scenario": {"image": "x:1"}}, runs=1,
                        env=[{name: "x"}])


def test_an_ordinary_variable_is_still_allowed():
    """The channel into a stack that reads its configuration from the environment stays
    open; this guard is about RoboVAST's own protocol, not about locking the block down."""
    config = ExecutionConfig(containers={"scenario": {"image": "x:1"}}, runs=1,
                             env=[{"ROS_DOMAIN_ID": "42"}, {"NAV2_PROFILE": "fast"}])
    assert config.env == [{"ROS_DOMAIN_ID": "42"}, {"NAV2_PROFILE": "fast"}]
