# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``robovast.common`` re-exports lazily, and both halves of that must stay true.

Python runs a package ``__init__`` before any submodule, so eager re-exports here were
paid by every module underneath: ``import robovast.common.status`` -- a pydantic model
with no other dependency -- pulled ``numpy`` and ``scenario_execution`` and cost 528
modules. That is what makes a light client impossible, and it is invisible until someone
measures.

Two failure modes to guard. The re-exports can stop working (a name in ``_LAZY`` pointing
at the wrong submodule fails only when someone imports that one name), and the laziness
can be lost (one eager import added back at the top of ``__init__`` restores the old cost
for everything). The first is checked by resolving every name; the second in a subprocess,
since ``sys.modules`` in a process that has already run a suite proves nothing.
"""

import json
import subprocess
import sys
import textwrap

import pytest

import robovast.common as common

#: Pulled in by ``.common``/``.config_generation``/``.execution``; a light submodule
#: import must not reach them.
HEAVY = ("numpy", "scenario_execution", "pandas")


def _import_in_subprocess(statement: str) -> dict:
    script = textwrap.dedent(f"""
        import json, sys
        {statement}
        print(json.dumps({{"mods": sorted(m for m in sys.modules if "." not in m),
                           "count": len(sys.modules)}}))
    """)
    out = subprocess.run([sys.executable, "-c", script],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_all_matches_the_lazy_table():
    assert sorted(common.__all__) == sorted(common._LAZY)  # noqa: SLF001


@pytest.mark.parametrize("name", sorted(common._LAZY))  # noqa: SLF001
def test_every_re_exported_name_resolves(name):
    """A wrong submodule in ``_LAZY`` breaks only the one caller that wants that name."""
    assert getattr(common, name) is not None


def test_an_unknown_name_still_raises_attribute_error():
    """``__getattr__`` must not turn a typo into an ImportError from somewhere else."""
    with pytest.raises(AttributeError, match="no attribute"):
        common.definitely_not_exported  # noqa: B018, PLW0104


@pytest.mark.parametrize("heavy", HEAVY)
def test_importing_a_light_submodule_stays_light(heavy):
    result = _import_in_subprocess("import robovast.common.status")
    assert heavy not in result["mods"], (
        f"`import robovast.common.status` pulled `{heavy}`. Something in "
        f"robovast/common/__init__.py imports eagerly again; the re-exports have to go "
        f"through `_LAZY` and `__getattr__`.")


def test_the_light_submodule_import_stays_small():
    """A ceiling, not a target: it was 528 before the re-exports went lazy."""
    result = _import_in_subprocess("import robovast.common.status")
    assert result["count"] < 400, (
        f"`import robovast.common.status` now costs {result['count']} modules (was 262 "
        f"after the untangling, 528 before it).")


def test_the_re_exports_still_work_for_callers():
    """53 call sites use this spelling; laziness must be invisible to them."""
    result = _import_in_subprocess(
        "from robovast.common import load_config, VariationConfig, is_campaign_dir")
    assert result["count"] > 0  # it imported at all, i.e. the names resolved


def test_the_client_closure_does_not_import_the_in_process_server():
    """`campaign_wait` + `service_target` + `login` is what a thin client needs.

    They reach `RobovastClient` through `service.client`, which used to re-export the
    3,000-line in-process server eagerly -- so asking "is this campaign done yet?" pulled
    the whole local Docker lane. A client distribution would not even have that module.
    """
    result = _import_in_subprocess(
        "import robovast.execution.campaign_wait, robovast.common.cli.service_target, "
        "robovast.common.cli.login")
    for heavy in HEAVY:
        assert heavy not in result["mods"], f"the client closure pulled {heavy}"
    assert result["count"] < 400, (
        f"the client closure now costs {result['count']} modules (was 286).")
