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

import logging
from importlib.metadata import entry_points
from typing import Dict

logger = logging.getLogger(__name__)


def load_variation_classes() -> Dict[str, type]:
    """Load variation classes from the ``robovast.variation_types`` entry-point group."""
    classes: Dict[str, type] = {}
    try:
        eps = entry_points(group="robovast.variation_types")
        for ep in eps:
            try:
                classes[ep.name] = ep.load()
            except Exception as e:
                logger.warning("Failed to load variation class '%s': %s", ep.name, e)
    except Exception:
        pass
    return classes
