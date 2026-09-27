# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A configuration's identity covers every input its composition read.

The ``.vast`` names a map YAML; only the variation knows the image that YAML points at, so
the identity takes it from the files the variation reported reading. A variation's own source
is hashed from its package, which for a workspace plugin is read from the plugin venv without
importing it.
"""

import os
import sys
import textwrap

import pytest
import yaml

from robovast.common.config_generation import (UnknownVariationClass,
                                               generate_scenario_variations)
from robovast.common.config_identifier import (VariationSourceNotFound,
                                               _source_root,
                                               compute_config_identifier,
                                               hash_variation_entrypoints)
from robovast.common.config_plugins import (MARKER_NAME, plugin_dir,
                                            plugin_site_dir)
from robovast.common.execution import prepare_campaign_configs

from .test_path_variation_cache import _project
from .test_path_variation_cache_map_image import _redraw_image

_HIT = "Loaded configurations from cache"


def _identity(vast, tmp_path, name):
    """Compose *vast*, stage it, and return ``(config.yaml, served from cache)``."""
    messages = []
    data = generate_scenario_variations(str(vast), progress_update_callback=messages.append,
                                        output_dir=str(tmp_path / f"gen-{name}"))
    out = tmp_path / f"campaign-{name}"
    prepare_campaign_configs(str(out), data)
    (config,) = data["configs"]
    record = yaml.safe_load((out / config["name"] / "_config" / "config.yaml").read_text())
    return record, any(_HIT in m for m in messages)


def test_a_redrawn_map_image_changes_the_config_identifier(tmp_path):
    vast = _project(tmp_path)
    before, _ = _identity(vast, tmp_path, "a")
    assert "read_files" in before["sub_identifier"]
    _redraw_image(tmp_path)
    after, _ = _identity(vast, tmp_path, "b")
    assert after["config_identifier"] != before["config_identifier"]


def test_a_cached_composition_keeps_the_config_identifier(tmp_path):
    """A hit carries the block and read files a fresh composition hashes."""
    vast = _project(tmp_path)
    fresh, fresh_hit = _identity(vast, tmp_path, "a")
    cached, cached_hit = _identity(vast, tmp_path, "b")
    assert (fresh_hit, cached_hit) == (False, True)
    assert cached == fresh


def test_an_unknown_variation_fails_the_identity(tmp_path):
    with pytest.raises(VariationSourceNotFound, match="NoSuchVariation"):
        compute_config_identifier(str(tmp_path), {"name": "c"}, "", "", ["NoSuchVariation"])


def test_an_unknown_variation_is_reported_by_composition(tmp_path):
    """The composition's own message names it, not the cache key's."""
    (tmp_path / "scenario.osc").write_text(
        "import osc.robotics\n\nscenario cell_test:\n    do serial:\n        wait elapsed(1s)\n")
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent("""\
        version: 6
        configuration:
        - name: cell
          variations:
          - NoSuchVariation: {}
        execution:
          containers:
            scenario: {image: scen:latest}
          runs: 1
          scenario_file: scenario.osc
        """))
    with pytest.raises(UnknownVariationClass, match="Unknown variation class 'NoSuchVariation'"):
        generate_scenario_variations(str(vast), output_dir=str(tmp_path / "out"))


def test_a_variation_under_a_namespace_root_hashes_its_package():
    """``robovast`` spans distributions; the variation ships in ``robovast.common``."""
    import robovast.common  # pylint: disable=import-outside-toplevel
    root = _source_root("robovast.common.variation.parameter_variation", None)
    assert root == os.path.dirname(robovast.common.__file__)


def _stage_plugin(vast_dir, source):
    """A workspace plugin installed the way pip lays it out, registering ``WsVariation``."""
    site = plugin_site_dir(str(vast_dir))
    package = os.path.join(site, "wsplug")
    os.makedirs(package, exist_ok=True)
    with open(os.path.join(package, "__init__.py"), "w", encoding="utf-8") as f:
        f.write(source)
    dist_info = os.path.join(site, "wsplug-1.0.dist-info")
    os.makedirs(dist_info, exist_ok=True)
    with open(os.path.join(dist_info, "METADATA"), "w", encoding="utf-8") as f:
        f.write("Metadata-Version: 2.1\nName: wsplug\nVersion: 1.0\n")
    with open(os.path.join(dist_info, "entry_points.txt"), "w", encoding="utf-8") as f:
        f.write("[robovast.variation_types]\nWsVariation = wsplug:WsVariation\n")
    marker = os.path.join(plugin_dir(str(vast_dir)), MARKER_NAME)
    with open(marker, "w", encoding="utf-8") as f:
        f.write("specs")
    stamp = os.stat(marker).st_mtime_ns
    os.utime(marker, ns=(stamp + len(source), stamp + len(source)))


def test_a_workspace_plugin_is_hashed_without_importing_it(tmp_path):
    _stage_plugin(tmp_path, "raise RuntimeError('imported')\n")
    before = hash_variation_entrypoints(["WsVariation"], str(tmp_path))
    assert "wsplug" not in sys.modules

    _stage_plugin(tmp_path, "raise RuntimeError('imported, and changed')\n")
    assert hash_variation_entrypoints(["WsVariation"], str(tmp_path)) != before
