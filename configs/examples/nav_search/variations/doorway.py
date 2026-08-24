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

"""A barrier across the room, with a doorway of searched width.

**Why a doorway and not a box.** A single obstacle in a 10 x 10 m room is not a hard
problem: nav2 has ~9.5 m of lateral freedom to route around a half-metre box, so it goes
around, essentially always. The only failures left are the degenerate ones -- crank the
lidar dropout until the box is invisible, or park it on the goal -- which is a threshold
on one variable dressed up as a search.

A doorway makes the *planner's* configuration decide the outcome. The classic nav2
phenomenon is that the robot physically fits and the planner refuses: both edges of the
gap are inflated, so the whole width carries lethal-adjacent cost. At the default
``inflation_radius: 0.55`` that already blocks gaps under roughly 1.1 m, while the TB4's
footprint is ~0.36 m. Sweeping the gap against the inflation radius therefore spans
always-refuses to always-passes, with the interesting structure on the diagonal between
them -- a genuine two-parameter interaction rather than a cliff in one.

The barrier is written to the **sim** channel because MuJoCo compiles its geometry before
the trial starts; nothing can create it later. It is deliberately absent from the map the
scenario hands nav2, so the global planner first routes straight through it and must
replan when the lidar sees it.

One parameter set produces exactly one configuration, as the search contract requires.
"""

from robovast.common.config import VariationConfig
from robovast.common.variation.base_variation import Variation


class DoorwayVariationConfig(VariationConfig):
    #: Clear width of the doorway [m]. Normally bound to a search_space dimension with a
    #: ``$name`` marker. Around 0.4 the TB4 barely fits; by 1.6 the default inflation
    #: radius stops objecting.
    gap_width: float
    #: Lateral centre of the doorway [m]. A constant by default -- the doorway sits on the
    #: straight line from start to goal, so width is the only geometry the search touches
    #: and a failure is attributable to it alone.
    gap_y: float = 0.0
    #: Half-width of the room [m]; the barrier spans wall to wall. Matches the built-in
    #: ``empty_room`` (10 x 10 m), and is stated rather than assumed because a barrier
    #: sized for the wrong room would leave a gap at one end that the robot slips through
    #: -- which looks like a passable doorway and is not one.
    room_half: float = 5.0
    #: Barrier thickness [m] and height [m]. The height only has to exceed the lidar's
    #: mount so the scan sees a wall rather than empty space above it.
    thickness: float = 0.2
    height: float = 1.0


class DoorwayVariation(Variation):
    """Two wall segments with a gap between them, written to the compiled world."""

    CONFIG_CLASS = DoorwayVariationConfig

    def variation(self, in_configs):
        p = self.parameters
        half = p.gap_width / 2.0
        lower_end = p.gap_y - half          # barrier runs from -room_half to here
        upper_start = p.gap_y + half        # ...and from here to +room_half

        instances = []
        for start, end in ((-p.room_half, lower_end), (upper_start, p.room_half)):
            length = end - start
            if length <= 0:
                # A doorway wider than the room leaves that side with no wall at all.
                # Emitting a zero- or negative-length box would be a geom MuJoCo rejects
                # or silently mangles; leaving the segment out is what the numbers
                # actually describe.
                continue
            instances.append({
                "pos": [0.0, round(start + length / 2.0, 4)],
                "size": [p.thickness, round(length, 4), p.height],
            })

        self.progress_update(
            f"doorway {p.gap_width:.2f} m at y={p.gap_y:.2f} "
            f"-> {len(instances)} barrier segment(s)")
        # One output per input: the search's 1:1 contract. `sim_values` and not scenario
        # parameters because the geometry has to exist in the compiled model before the
        # trial starts -- the scenario cannot create it later.
        return [
            self.update_config(
                config, {},
                sim_values={"components.barrier.instances": instances})
            for config in in_configs
        ]
