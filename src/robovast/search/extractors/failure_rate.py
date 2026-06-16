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

"""Built-in objective-only extractor: fraction of a config's runs that failed.

``failure_rate = failures / completed_runs`` in ``[0, 1]`` (the explicit
``runs > 1`` aggregation contract). Higher == harder, so maximizing it surfaces
the most failure-prone parameter sets. SUT-agnostic — reads only ``test.xml``,
which every scenario produces. No measures (use a custom extractor for QD).
"""

import logging
from pathlib import Path

from robovast.common.campaign_data import read_test_result

from ..extractor import Extractor, ExtractResult, completed_run_dirs

logger = logging.getLogger(__name__)


class FailureRate(Extractor):
    def extract(self, config_dir: Path) -> ExtractResult:
        completed = completed_run_dirs(config_dir)
        if not completed:
            logger.warning("No completed runs with results in %s; failure_rate=0.0", config_dir)
            return ExtractResult(objectives={"failure_rate": 0.0})
        failures = sum(1 for run_dir in completed if not read_test_result(run_dir)["success"])
        return ExtractResult(objectives={"failure_rate": failures / len(completed)})
