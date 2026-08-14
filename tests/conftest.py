# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""RoboVAST is standalone: its suite must be green with no simulator installed.

``pip install robovast`` deliberately names no simulator — a backend arrives through its
own package and registers a ``robovast.simulators`` entry point (``roqsim`` is one, and a
third-party one would arrive the same way). That is a real claim about the packaging, and
the honest way to keep it true is for the tests to *demonstrate* it rather than assert it
in prose.

So the tests that genuinely need a registered backend are marked ``requires_simulator``
and **skip** when none is installed. The alternative — which is what happened — is ~25
tests failing with "Unknown robovast.simulators plugin", an incomplete environment
reported as broken code, sending the reader to the wrong file entirely.

``make venv`` installs ``src/robovast_sim_roqsim``, so a developer sees them run; they
skip only where robovast really is standing alone.
"""

from importlib.metadata import entry_points

import pytest

#: Entry-point group a simulator backend registers itself in.
SIMULATOR_GROUP = "robovast.simulators"


@pytest.fixture(autouse=True)
def _isolated_login_config(tmp_path, monkeypatch):
    """No test may read or write the developer's real ``vast login``.

    ``detected_service_url()`` resolves local probe -> stored login, so any test that
    touches it silently depends on whether the person running the suite happens to be
    logged in. Three tests in ``test_service_target.py`` asserted "nothing answers, so
    the target is empty" and began failing the moment a real login existed -- a green
    suite on a fresh checkout, red on the maintainer's machine, for no change in the
    code.

    ``test_login.py`` had this fixture locally, which is exactly why the gap survived:
    per-module isolation protects the module that thought of it and nothing else. It
    belongs here, where it covers every test that will ever reach for a credential.
    """
    from robovast.common.cli import login  # pylint: disable=import-outside-toplevel
    monkeypatch.setenv(login.CONFIG_ENV_VAR, str(tmp_path / "robovast-login.json"))


def simulator_backends() -> list[str]:
    """Registered simulator backends, by name."""
    return sorted(e.name for e in entry_points(group=SIMULATOR_GROUP))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "requires_simulator: needs a registered robovast.simulators backend "
        "(installed by `make venv`); skipped when robovast stands alone")


def pytest_collection_modifyitems(config, items):
    if simulator_backends():
        return
    skip = pytest.mark.skip(
        reason=f"no {SIMULATOR_GROUP} backend installed — robovast is standalone, so "
               "these skip rather than fail. `make venv` (or `pip install -e "
               "src/robovast_sim_roqsim`) installs one.")
    for item in items:
        if "requires_simulator" in item.keywords:
            item.add_marker(skip)
