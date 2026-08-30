# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A campaign carries the local plugin modules its own configuration references.

A ``<path>.py:<Class>`` reference is the escape hatch for logic dropped next to a ``.vast``
without packaging it, and composition loads it every time -- a variation while expanding the
sweep, a strategy and an extractor at the top of every search batch. None of it was collected,
so ``<campaign>/_config/`` never carried it, and a retrigger (which re-composes from that
snapshot rather than replaying recorded configs) died resolving a file that was never archived.

These pin the collection, and the two boundaries around it: a module named as a mapping KEY is
a plugin, an arbitrary scenario parameter that merely looks like one is not -- the second
matters because run files are content-hashed into the config identity, so a false positive
would let user data decide whether two campaigns describe the same experiment.
"""

import textwrap

import yaml

from robovast.common.config_generation import generate_scenario_variations

_SCENARIO = """\
import osc.robotics

scenario nav:
    do serial:
        wait elapsed(1s)
"""

#: Minimal local plugin, mirroring ``test_variation_fileref.LOCAL_VARIATION``.
_VARIATION = textwrap.dedent("""\
    from robovast.common.variation.base_variation import Variation

    class TagVariation(Variation):
        def variation(self, in_configs):
            return [self.update_config(c, {"tag": "x"}) for c in in_configs]
""")


def _project(tmp_path, *, config=None, search=None, results=None, files=None):
    """Write a composable project and return its ``.vast`` path."""
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    for rel, body in (files or {}).items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)

    document = {
        "version": 3,
        "metadata": {"name": "archived"},
        "execution": {
            "containers": {"sut": {"image": "sut:latest"},
                           "scenario": {"image": "scen:latest"}},
            "scenario_file": "scenario.osc",
            "runs": 1,
        },
    }
    if config is not None:
        document["configuration"] = config
    if search is not None:
        document["search"] = search
    if results is not None:
        document["results_processing"] = results

    vast = tmp_path / "campaign.vast"
    vast.write_text(yaml.safe_dump(document, sort_keys=False))
    return vast


def _compose(vast, tmp_path):
    return generate_scenario_variations(
        str(vast), progress_update_callback=lambda _m: None,
        output_dir=str(tmp_path / "out"), use_cache=False)


def test_a_file_ref_variation_lands_in_the_run_files(tmp_path):
    """The reported bug: the module a variation is defined in was never carried."""
    vast = _project(
        tmp_path,
        config=[{"name": "base",
                 "variations": [{"variations/myvar.py:TagVariation": {}}]}],
        files={"variations/myvar.py": _VARIATION})

    assert "variations/myvar.py" in _compose(vast, tmp_path)["_run_files"]


def test_a_oneof_child_file_ref_is_archived_too(tmp_path):
    """Nesting is found by the same recursion, with no list of shapes to keep current."""
    vast = _project(
        tmp_path,
        config=[{"name": "base", "variations": [
            {"OneOfVariation": {
                "variations": [{"variations/myvar.py:TagVariation": {}}]}}]}],
        files={"variations/myvar.py": _VARIATION})

    assert "variations/myvar.py" in _compose(vast, tmp_path)["_run_files"]


def test_an_entry_point_variation_adds_no_run_file(tmp_path):
    """An installed plugin is not a file, and must not invent one."""
    vast = _project(
        tmp_path,
        config=[{"name": "base", "variations": [
            {"ParameterVariationList": {"scenario": "speed", "values": [1, 2]}}]}])

    assert _compose(vast, tmp_path)["_run_files"] == []


def test_a_scenario_parameter_that_looks_like_a_ref_is_not_collected(tmp_path):
    """User data must not reach the identity hash, however much it resembles a reference.

    ``configuration[].parameters`` is arbitrary, so a value is only read as a plugin under a
    key that names one. Here the file even EXISTS, which is what makes the assertion mean
    something: nothing but the key position separates it from a real reference.
    """
    vast = _project(
        tmp_path,
        config=[{"name": "base",
                 "parameters": [{"entrypoint": "tools/run.py:main"}]}],
        files={"tools/run.py": "def main():\n    pass\n"})

    assert _compose(vast, tmp_path)["_run_files"] == []


def test_a_ref_naming_no_file_is_left_to_composition(tmp_path):
    """Collection stays silent so the resolver reports it, naming the path it looked at."""
    from robovast.common.config_generation import _plugin_run_files
    from robovast.common.common import load_config

    vast = _project(
        tmp_path,
        config=[{"name": "base",
                 "variations": [{"variations/absent.py:TagVariation": {}}]}])

    assert _plugin_run_files(str(tmp_path), load_config(str(vast))) == []


def test_a_ref_that_escapes_the_project_is_not_collected(tmp_path):
    """A campaign archives its own tree; a path climbing out of it is not part of it."""
    from robovast.common.config_generation import _plugin_run_files
    from robovast.common.common import load_config

    (tmp_path.parent / "outside.py").write_text(_VARIATION)
    vast = _project(
        tmp_path,
        config=[{"name": "base",
                 "variations": [{"../outside.py:TagVariation": {}}]}])

    assert _plugin_run_files(str(tmp_path), load_config(str(vast))) == []


def test_a_postprocessing_ref_stays_an_input_file(tmp_path):
    """Two destinations, and the rule that decides between them.

    A results plugin runs after the campaign; the campaign runs without it. It is archived so
    the results view can rebuild, but it is NOT part of what the experiment is, so it must not
    reach the config identity.
    """
    vast = _project(
        tmp_path,
        config=[{"name": "base"}],
        results={"postprocessing": ["analysis/render.py:Render"]},
        files={"analysis/render.py": "class Render:\n    pass\n"})

    data = _compose(vast, tmp_path)
    assert "analysis/render.py" in data["_input_files"]
    assert "analysis/render.py" not in data["_run_files"]
