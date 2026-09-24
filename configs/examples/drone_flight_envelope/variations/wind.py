# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Wind speed, heading and turbulence -> the `steady` vector and `turbulence` block of the
`wind_field` plugin. Two slots, because a list or mapping destination is replaced wholesale::

    - variations/wind.py:WindVariation:
        wind_speed: $wind_speed
        heading_deg: $wind_heading
        turbulence: $turbulence
        sim:
          steady:     components.wind_field.steady
          turbulence: components.wind_field.turbulence
"""

from __future__ import annotations

import math

from robovast.common.config import VariationConfig
from robovast.common.variation.base_variation import Variation


class WindVariationConfig(VariationConfig):
    wind_speed: float          # m/s
    heading_deg: float = 0.0   # direction the wind blows towards, CCW from +x
    turbulence: float = 0.0    # Dryden sigma, m/s
    length_scale: float = 4.0  # m
    vertical: float = 0.5      # sigma scale on the vertical axis


class WindVariation(Variation):
    CONFIG_CLASS = WindVariationConfig
    SLOTS = ("steady", "turbulence")

    def variation(self, in_configs):
        p = self.parameters
        heading = math.radians(p.heading_deg)
        # Horizontal only: a mean vertical wind would confound the payload factor.
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
