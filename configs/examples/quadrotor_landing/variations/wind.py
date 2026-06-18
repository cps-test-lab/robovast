# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""A local quadrotor variation plugin: a wind model.

This is referenced from the search vasts as a local file plugin
(``variations/wind.py:WindFieldVariation``) to exercise the search
``variations:`` template on the self-contained quadrotor example.

Rather than searching the simulator's raw ``wind_strength`` acceleration directly,
the search proposes a physical **wind speed** (m/s) and a **turbulence** margin;
this variation converts them into the ``wind_strength`` the sim consumes using a
quadratic-drag law (wind force ∝ speed²)::

    wind_strength = drag_k * wind_speed**2 * (1 + turbulence)

So the searched space is physically meaningful, and a real computation (not a
passthrough) sits between the search and the simulator — exactly what the
``variations:`` template is for. The variation is deterministic and produces
exactly one config per input, satisfying the search 1:1 contract.
"""

from robovast.common.config import VariationConfig
from robovast.common.variation.base_variation import Variation


class WindFieldVariationConfig(VariationConfig):
    # Physical wind speed [m/s] and a unitless gust margin; both are normally
    # bound to a search_space variable via a ``$name`` marker in the .vast.
    wind_speed: float
    turbulence: float = 0.0
    # Lumped drag coefficient mapping wind_speed² -> acceleration. The default
    # maps wind_speed in [0, 12] m/s onto roughly the sim's original
    # wind_strength range [0, ~5] m/s².
    drag_k: float = 0.035
    # The scenario parameter this variation writes (the sim reads wind_strength).
    param_name: str = "wind_strength"


class WindFieldVariation(Variation):
    """Derive ``wind_strength`` from a physical wind speed + turbulence margin."""

    CONFIG_CLASS = WindFieldVariationConfig

    def variation(self, in_configs):
        p = self.parameters
        wind_strength = round(p.drag_k * p.wind_speed ** 2 * (1.0 + p.turbulence), 4)
        self.progress_update(
            f"wind_speed={p.wind_speed} m/s, turbulence={p.turbulence} "
            f"-> {p.param_name}={wind_strength} m/s^2")
        # One output config per input (no expansion): the search 1:1 contract.
        return [self.update_config(config, {p.param_name: wind_strength})
                for config in in_configs]
