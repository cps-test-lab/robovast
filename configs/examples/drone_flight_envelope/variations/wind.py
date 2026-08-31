# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A local variation plugin: physical weather -> the simulator's wind field.

The search proposes a **wind speed** (m/s), a **heading** (degrees) and a **turbulence intensity**;
this turns them into the two config values ``roqsim_aerial``'s ``wind_field`` plugin actually reads.
That conversion is the point of the ``variations:`` template: the searched space stays in units a
drone engineer reasons about, while the simulator gets the shape it needs.

Referenced from a ``.vast`` as a local file plugin::

    - variations/wind.py:WindVariation:
        wind_speed: $wind_speed
        heading_deg: $wind_heading
        turbulence: $turbulence
        sim:
          steady:     components.wind_field.steady
          turbulence: components.wind_field.turbulence

Two outputs, so the plugin declares :attr:`SLOTS` and the ``.vast`` binds each by name. They cannot
be one value: ``steady`` is a 3-vector and ``turbulence`` is a mapping, and there is no list- or
partial-mapping addressing in the override path -- a mapping destination is replaced wholesale.

Deterministic, and exactly one output config per input, which is the contract a search variation
must satisfy.
"""

from __future__ import annotations

import math

from robovast.common.config import VariationConfig
from robovast.common.variation.base_variation import Variation


class WindVariationConfig(VariationConfig):
    #: Mean wind speed [m/s]. Normally bound to a search_space variable with ``$name``.
    wind_speed: float
    #: Where the wind blows *towards*, in degrees CCW from +x. 0 pushes the drone east.
    heading_deg: float = 0.0
    #: Dryden turbulence sigma [m/s]. 0 disables turbulence entirely.
    turbulence: float = 0.0
    #: Dryden length scale [m]. Larger is slower, more correlated gusting.
    length_scale: float = 4.0
    #: Sigma scale on the vertical axis, as Dryden's low-altitude form has it.
    vertical: float = 0.5


class WindVariation(Variation):
    """Turn (speed, heading, turbulence) into a ``wind_field`` steady vector and turbulence block."""

    CONFIG_CLASS = WindVariationConfig
    SLOTS = ("steady", "turbulence")

    def variation(self, in_configs):
        p = self.parameters
        heading = math.radians(p.heading_deg)
        # Horizontal only: a mean vertical wind is a draught, not weather, and it would trade
        # against the thrust margin -- the very thing the payload factor is there to vary. Keeping
        # it at zero stops the two factors from confounding each other.
        steady = [
            round(p.wind_speed * math.cos(heading), 4),
            round(p.wind_speed * math.sin(heading), 4),
            0.0,
        ]
        turbulence = {
            "intensity": round(float(p.turbulence), 4),
            "length_scale": float(p.length_scale),
            "vertical": float(p.vertical),
        }
        self.progress_update(
            f"wind {p.wind_speed} m/s @ {p.heading_deg} deg, turbulence sigma={p.turbulence} "
            f"-> steady={steady}"
        )
        return [
            self.update_config(
                config, {}, sim_values={"steady": steady, "turbulence": turbulence}
            )
            for config in in_configs
        ]
