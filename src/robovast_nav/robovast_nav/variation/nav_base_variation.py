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
from typing import Optional

from robovast.common import is_scenario_parameter
from robovast.common.variation import Variation


class NavVariation(Variation):

    def get_map_file(self, map_file_parameter, config) -> Optional[str]:
        """Determine the map file path to use for this config."""

        map_file_path = None
        # 1. try to get map file from config
        if map_file_parameter:
            temp_path = os.path.join(self.base_path, map_file_parameter)
            if os.path.exists(temp_path):
                # 1.1. found map file directly
                self.progress_update(f"Using map file from configuration: {temp_path}")
                map_file_path = temp_path
            else:
                # 2. try to resolve from scenario parameter
                self.progress_update(f"Map file {map_file_parameter} does not exist. Using it as scenario parameter reference.")
                if not is_scenario_parameter(map_file_parameter, self.scenario_file):
                    raise ValueError(f"Map file {map_file_path} is not a valid scenario parameter reference.")
                if map_file_parameter in config["config"]:
                    temp_path = os.path.join(config["config"][map_file_parameter])
                    if os.path.exists(temp_path):
                        self.progress_update(f"Resolved map file path from scenario parameter: {temp_path}")
                        map_file_path = temp_path
                    else:
                        raise FileNotFoundError(f"Resolved map file path from scenario parameter does not exist: {temp_path}")
        else:
            self.progress_update("No map_file specified in PathVariation configuration.")
            if "_map_file" in config:
                temp_path = config["_map_file"]
                if os.path.exists(temp_path):
                    self.progress_update(f"Using map file from config._map_file: {temp_path}")
                    map_file_path = temp_path
                else:
                    raise FileNotFoundError(f"Map file from config data does not exist: {temp_path}")

        if not map_file_path:
            raise ValueError("No valid map file path could be determined.")

        return map_file_path
