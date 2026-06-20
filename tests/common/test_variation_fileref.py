# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Variations load from entry points OR a local ``./path.py:Class`` file ref.

Parity with search strategies/extractors and results postprocessing: a variation
named in a ``.vast`` resolves either to an installed ``robovast.variation_types``
entry point or to a local file plugin relative to the ``.vast`` directory.
"""

# Skipif-guarded tests import the plugin loader lazily.
# pylint: disable=import-outside-toplevel

import os
import textwrap

import pytest

from robovast.common.config_generation import _get_variation_classes

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
QUAD = os.path.join(REPO, "configs", "examples", "quadrotor_landing")

LOCAL_VARIATION = textwrap.dedent("""\
    from robovast.common.variation.base_variation import Variation

    class TagVariation(Variation):
        def variation(self, in_configs):
            return [self.update_config(c, {"tag": "x"}) for c in in_configs]
""")


def test_local_variation_file_ref_resolves(tmp_path):
    (tmp_path / "myvar.py").write_text(LOCAL_VARIATION)
    classes = _get_variation_classes(
        {"variations": [{"myvar.py:TagVariation": {}}]}, str(tmp_path))
    assert len(classes) == 1
    cls, _ = classes[0]
    assert cls.__name__ == "TagVariation"


def test_unknown_variation_name_raises(tmp_path):
    with pytest.raises(ValueError, match="Unknown variation class"):
        _get_variation_classes({"variations": [{"NoSuchVariation": {}}]}, str(tmp_path))


def test_non_variation_file_ref_raises(tmp_path):
    (tmp_path / "bad.py").write_text("class NotAVariation:\n    pass\n")
    with pytest.raises(ValueError, match="not a subclass of Variation"):
        _get_variation_classes(
            {"variations": [{"bad.py:NotAVariation": {}}]}, str(tmp_path))


@pytest.mark.skipif(
    not os.path.exists(os.path.join(QUAD, "variations", "wind.py")),
    reason="quadrotor wind variation example not available")
def test_windfield_variation_computes_wind_strength(tmp_path):
    from robovast.common.plugin_ref import load_ref
    cls = load_ref("variations/wind.py:WindFieldVariation",
                   "robovast.variation_types", QUAD)
    var = cls(QUAD, {"wind_speed": 10.0, "turbulence": 0.2}, {},
              lambda *_: None, None, str(tmp_path))
    out = var.variation([{"name": "c", "config": {}}])
    assert len(out) == 1  # one config per input (1:1 search contract)
    # wind_strength = drag_k * wind_speed**2 * (1 + turbulence) = 0.035*100*1.2
    assert out[0]["config"]["wind_strength"] == pytest.approx(4.2)
    # Deterministic: a second run yields the same value.
    again = var.variation([{"name": "c", "config": {}}])
    assert again[0]["config"]["wind_strength"] == pytest.approx(4.2)
