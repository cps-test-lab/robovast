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

"""Entry-point group names for search plugins, plus the shared ``load_ref``.

Plugins resolve through :func:`robovast.common.plugin_ref.load_ref`, so every
search plugin reference may be either an installed entry-point name or a local
file relative to the ``.vast``.
"""

from robovast.common.plugin_ref import load_ref  # re-exported for convenience

STRATEGY_GROUP = "robovast.search_strategies"
EXTRACTOR_GROUP = "robovast.extractors"

__all__ = ["load_ref", "STRATEGY_GROUP", "EXTRACTOR_GROUP"]
