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

"""``extract_to_csv`` postprocessing plugin.

Runs a search :class:`~robovast.search.extractor.Extractor` over each config in a
campaign and writes its objectives + measures to a per-config CSV (default
``metrics.csv``). This lets the **same extractor** that scores a search also feed
the analysis/visualization notebooks for batch runs — one source of truth, no
duplicated metric logic.

Config (in ``results_processing.postprocessing``):

    postprocessing:
    - extract_to_csv:
        plugin: ./search/extract.py:QuadExtract   # entry-point name or file ref
        params: {crash_speed: 1.0}                 # extractor params
        file: metrics.csv                          # output name (default)
"""

import csv
import logging
import os
from typing import Tuple

from robovast.common.plugin_ref import load_ref

from .postprocessing_plugins import BasePostprocessingPlugin

logger = logging.getLogger(__name__)

EXTRACTOR_GROUP = "robovast.extractors"
_RESERVED = {"_config", "_execution", "_transient", "_jobs"}


class ExtractToCsv(BasePostprocessingPlugin):
    def __call__(self, results_dir: str, config_dir: str, plugin: str = None,
                 params: dict = None, file: str = "metrics.csv", **kwargs) -> Tuple[bool, str]:
        if not plugin:
            return False, "extract_to_csv requires a 'plugin' (extractor ref)"
        try:
            extractor_cls = load_ref(plugin, EXTRACTOR_GROUP, config_dir)
            extractor = extractor_cls(**(params or {}))
        except Exception as e:  # pylint: disable=broad-except
            return False, f"extract_to_csv could not load extractor '{plugin}': {e}"

        written = 0
        for entry in sorted(os.listdir(results_dir)):
            cfg_path = os.path.join(results_dir, entry)
            if entry in _RESERVED or entry.startswith(".") or not os.path.isdir(cfg_path):
                continue
            from pathlib import Path
            try:
                result = extractor.extract(Path(cfg_path))
            except Exception as e:  # pylint: disable=broad-except
                logger.warning("extract_to_csv: extractor failed on %s: %s", cfg_path, e)
                continue
            row = {**result.objectives, **result.measures}
            with open(os.path.join(cfg_path, file), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(list(row.keys()))
                w.writerow([row[k] for k in row])
            written += 1
        return True, f"extract_to_csv wrote {file} for {written} config(s)"
