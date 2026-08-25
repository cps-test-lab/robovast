# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A notebook workload may not be named after the Explorer's built-in Log tab.

The Explorer appends a **Log** tab after whatever the campaign declared, at run level, and it is
not declarable -- a run always has a log, so there is nothing for a ``.vast`` to decide. A workload
of the same name therefore renders a second tab that reads the same, and nothing on screen says
which is which.

Pinned here because that failure is silent in both directions. Nothing errors: the tab bar simply
grows two entries a reader has to guess between. And the URL addresses a tab by its name
(``#/results/explorer/<campaign>/<config>/<run>?tab=log``), so the collision would also make a
shared link ambiguous -- which is exactly the kind of thing that gets "fixed" downstream by
disambiguating at the point of use, in two places, differently.
"""

import pytest
from pydantic import ValidationError

from robovast.common.config import EXPLORER_SCOPES, ExplorerConfig

RESERVED = ExplorerConfig.RESERVED_WORKLOAD_NAMES


def _notebooks(name):
    """An ``explorer.notebooks`` block declaring one workload called *name*."""
    return {"notebooks": [{name: {"run": "report.ipynb"}}]}


def test_an_ordinary_workload_is_accepted():
    cfg = ExplorerConfig.model_validate(_notebooks("nav_report"))
    assert cfg.notebooks == [{"nav_report": {"run": "report.ipynb"}}]


@pytest.mark.parametrize("name", ["log", "Log", "LOG"])
def test_the_built_in_tab_name_is_refused_in_any_casing(name):
    # Case-insensitively: the tabs are labelled by the declared name, so `log` beside the built-in
    # `Log` is the very pair a reader cannot tell apart.
    with pytest.raises(ValidationError) as excinfo:
        ExplorerConfig.model_validate(_notebooks(name))
    # The message has to name the built-in tab, or the only reading left is "this name is banned".
    assert "Log tab" in str(excinfo.value)
    assert name in str(excinfo.value)


def test_every_reserved_name_is_refused():
    # Whatever the list holds, not just the one name it holds today.
    for name in RESERVED:
        with pytest.raises(ValidationError):
            ExplorerConfig.model_validate(_notebooks(name))


def test_nothing_declared_is_still_valid():
    # The whole block is optional; a campaign that declares no notebooks has no workloads to clash.
    assert ExplorerConfig.model_validate({}).notebooks is None


def test_every_addressable_scope_is_accepted():
    # The set the renderer selects on. A scope missing from here would be refused in a `.vast`
    # while still rendering, which is the inverse of the bug below and just as confusing.
    scopes = {scope: f"{scope}.ipynb" for scope in EXPLORER_SCOPES}
    cfg = ExplorerConfig.model_validate({"notebooks": [{"nav_report": scopes}]})
    assert cfg.notebooks == [{"nav_report": scopes}]


@pytest.mark.parametrize("scope", ["single_test", "runs", "Run"])
def test_a_scope_the_explorer_cannot_address_is_refused(scope):
    # The renderer keeps the scopes it knows and ignores the rest, so an unrecognised key left
    # the notebook declared, staged and never rendered -- no tab, no error, nothing to read.
    # `single_test` is the real case: it was the name of the run scope before the rename, and a
    # config in this repo's dataset still carried it long after it stopped meaning anything.
    with pytest.raises(ValidationError) as excinfo:
        ExplorerConfig.model_validate({"notebooks": [{"nav_report": {scope: "r.ipynb"}}]})
    message = str(excinfo.value)
    assert scope in message
    # ...and the message must say what IS addressable, or the author has nothing to correct to.
    for known in EXPLORER_SCOPES:
        assert known in message


def test_a_valid_scope_beside_an_invalid_one_still_fails():
    # Partial acceptance is the failure mode being removed: the good scope would render and the
    # typo'd one would vanish, which reads as "the notebook works" from the tab bar.
    with pytest.raises(ValidationError):
        ExplorerConfig.model_validate(
            {"notebooks": [{"nav_report": {"run": "r.ipynb", "single_test": "s.ipynb"}}]})
