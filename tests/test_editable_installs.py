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
    "robovast.client.cluster_cli": "robovast-client (src/robovast_client)",
    "robovast.client.campaign_cli": "robovast-client (src/robovast_client)",
    "robovast.client.service_cli": "robovast-client (src/robovast_client)",
    "robovast.client.container_cli": "robovast-client (src/robovast_client)",
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


#: Entry-point groups where two distributions declaring the same name is a real hazard
#: rather than a merge: the loader builds ``{ep.name: ep}``, so a duplicate resolves
#: unpredictably to whichever came last.
SINGLE_PROVIDER_GROUPS = ("robovast.cli_plugins", "robovast.cluster_plugins",
                          "robovast.service_plugins")


@pytest.mark.parametrize("group", SINGLE_PROVIDER_GROUPS)
def test_no_entry_point_name_has_two_providers(group):
    """Stale metadata is invisible until it resolves the wrong way.

    Entry points live in *installed* metadata, so moving one between distributions leaves
    the old declaration behind until every dist is reinstalled. ``cluster`` was moved from
    ``robovast-cluster`` to ``robovast-client`` exactly this way; in the window before a
    reinstall both declared it and which one won was unspecified. ``service`` now spans two
    distributions by design, which is why this checks for duplicate *names* rather than for
    a group having one provider. Nothing else in the suite can see that: the modules are fine,
    the imports are fine, and the CLI lists the verb either way.
    """
    from collections import Counter
    from importlib.metadata import entry_points

    seen = Counter(ep.name for ep in entry_points(group=group))
    duplicated = {name: count for name, count in seen.items() if count > 1}
    assert not duplicated, (
        f"{duplicated} declared more than once in '{group}'.\n"
        f"Two installed distributions claim the same verb, so which one runs is "
        f"unspecified. Usually stale metadata from a moved entry point: re-run "
        f"'make venv' (or 'pip install --no-deps -e .' in each of src/*)."
    )


def _declared_entry_point_groups():
    """Every ``robovast.*`` entry-point group the five manifests declare.

    Read out of the ``pyproject.toml`` files rather than listed here, so a group added
    tomorrow is covered without anyone remembering to add it.
    """
    import re

    root = Path(__file__).resolve().parent.parent
    manifests = [root / "pyproject.toml", *sorted(root.glob("src/*/pyproject.toml"))]
    groups = set()
    for manifest in manifests:
        groups.update(re.findall(r'\[tool\.poetry\.plugins\."(robovast\.[^"]+)"\]',
                                 manifest.read_text(encoding="utf-8")))
    assert groups, f"no entry-point groups found in {manifests}"
    return sorted(groups)


#: Declared with no entries on purpose (``pyproject.toml``), so "empty" is correct for it.
_DELIBERATELY_EMPTY = {"robovast.metadata_processing"}


@pytest.mark.parametrize("group", _declared_entry_point_groups())
def test_every_declared_group_has_a_provider(group):
    """A group robovast declares must not come back empty.

    This is the invariant the shadowing diagnosis in ``plugin_ref`` relies on: because an
    empty built-in group is impossible in a healthy install, an empty one is evidence of a
    broken *installation* rather than a missing plugin, and may be reported as such.

    It is also the assertion that would have caught the outage directly. A workspace
    plugin directory carrying its own ``robovast`` distribution replaced the host's entry
    points wholesale -- ``importlib.metadata`` deduplicates by distribution name and keeps
    the first on ``sys.path`` -- so ``robovast.search_strategies`` read ``(none
    registered)`` while every module and import in the process was fine.
    """
    from importlib.metadata import entry_points

    if group in _DELIBERATELY_EMPTY:
        pytest.skip(f"{group} is declared with no entries")
    assert list(entry_points(group=group)), (
        f"entry-point group {group!r} is empty. robovast declares it, so this is a broken "
        f"installation: either the metadata is stale (run `make venv`) or a second "
        f"distribution ahead of it on sys.path is shadowing it.")


def test_only_one_distribution_is_named_robovast():
    """Two would mean only one of them answers, silently, for every entry-point group."""
    from importlib.metadata import distributions

    from robovast.common.config_plugins import canonical_name

    same = [d for d in distributions()
            if canonical_name(d.metadata["Name"] or "") == "robovast"]
    where = [f"{d.version} at {d.locate_file('')}" for d in same]
    assert len(same) == 1, (
        "more than one distribution named 'robovast' is visible; only the first on "
        f"sys.path is ever used: {'; '.join(where)}")
