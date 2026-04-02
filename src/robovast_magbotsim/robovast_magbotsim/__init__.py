# Copyright (C) 2025 RoboVAST Contributors
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

"""MagBotSim environment variation types for RoboVAST."""

from .magbotsim_env_variation import MagBotSimEnvVariation, MagBotSimEnvVariationConfig

__all__ = [
    'MagBotSimEnvVariation',
    'MagBotSimEnvVariationConfig',
]

# Lazy import for GUI classes to avoid loading PySide6 in headless environments
def __getattr__(name):
    if name == 'MagBotSimEnvGui':
        from .gui import MagBotSimEnvGui  # pylint: disable=import-outside-toplevel
        return MagBotSimEnvGui
    elif name == 'MagBotSimTileLayoutWidget':
        from .gui import MagBotSimTileLayoutWidget  # pylint: disable=import-outside-toplevel
        return MagBotSimTileLayoutWidget
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
