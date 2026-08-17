# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The suite must be testing the source in this checkout, not a copy in site-packages.

This repo ships several distributions that all populate the ``robovast`` namespace
(``robovast``, ``robovast-client``, ``robovast-nav``, ``robovast-sim-roqsim``). If one of
them is installed **non-editably** its files land in ``site-packages/robovast/...`` and
shadow the tree being edited — for exactly the modules that distribution owns, and only
those, so most of the suite still exercises the checkout and nothing looks wrong.

That is not hypothetical. ``robovast-client`` is a *path dependency* of ``robovast``, so
installing any sibling (``pip install -e src/robovast_nav``) can quietly reinstall it as a
regular copy. Twice during one change the interface models were edited, the suite was run,
and it passed — against the previous version of those models, because pydantic ignores
fields a stale model does not declare. A green suite meant nothing.

``make venv`` installs each of them editable. This makes a violation fail loudly, here,
instead of as a mysterious result somewhere else.
"""

from pathlib import Path

import pytest

#: The checkout this test file lives in.
REPO = Path(__file__).resolve().parents[1]

#: module -> the distribution that owns it, for the message.
NAMESPACE_MODULES = {
    "robovast.common.config": "robovast",
    "robovast.service.interface": "robovast-client (src/robovast_client)",
    "robovast.service.project_push": "robovast-client (src/robovast_client)",
    "robovast.client.scene_markers": "robovast-client (src/robovast_client)",
    "robovast.service.local_transport": "robovast",
}


@pytest.mark.parametrize("module_name,distribution", sorted(NAMESPACE_MODULES.items()))
def test_the_suite_runs_against_this_checkout(module_name, distribution):
    import importlib

    module = importlib.import_module(module_name)
    location = Path(module.__file__).resolve()
    assert REPO in location.parents, (
        f"{module_name} was imported from {location}, not from this checkout.\n"
        f"{distribution} is installed as a *copy* rather than editable, so it shadows the "
        f"source being edited and the suite is testing the installed version.\n"
        f"Fix: pip install --no-deps -e src/<that package>  (or: make venv)"
    )
