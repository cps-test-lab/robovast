# Copyright (C) 2025 Frederik Pasch
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

import os

from robovast.common.variation import VariationGui

from .map_visualizer_qt import MapVisualizerWidget


class NavigationGui(MapVisualizerWidget, VariationGui):
    """GUI for Navigation Variations."""

    def update(self, variant, path):
        """Update the GUI with the given variant data.

        Args:
            variant: The variant data to display.
            path: The file path of the variant.
        """
        map_file = variant.get('_map_file', None)
        if os.path.isabs(map_file):
            map_path = map_file
        else:
            map_path = os.path.join(path, map_file)
        if map_file:
            print(f"Loading map file in GUI: {map_path}")
            self.load_map(map_path)
            # Clear any previous drawings (paths, obstacles, etc.) before renderers draw new content
            self.refresh()
