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

"""Common utilities for results directory layout (run-<id>/<config>/<test-number>)."""
from pathlib import Path
from typing import Iterator, Tuple


def iter_test_folders(results_dir: str) -> Iterator[Tuple[str, str, str, Path]]:
    """Iterate over all test folders under a results directory.

    Discovers the standard layout: results_dir/run-<id>/<config>/<test-number>/.
    Under results_dir, only directories whose name starts with 'run-' are
    considered; under each run, subdirs are config names; under each config,
    subdirs whose names are numeric are test numbers.

    Args:
        results_dir: Path to the project results directory (parent of run-* dirs).

    Yields:
        Tuples (run_id, config_name, test_number, folder_path) where folder_path
        is the full path to run-<id>/<config>/<test-number>.
    """
    root = Path(results_dir)
    if not root.is_dir():
        return

    for run_item in sorted(root.iterdir()):
        if not run_item.is_dir() or not run_item.name.startswith("run-"):
            continue
        if run_item.name == "_config":
            continue
        run_id = run_item.name

        for config_item in sorted(run_item.iterdir()):
            if not config_item.is_dir():
                continue
            config_name = config_item.name

            for test_item in sorted(config_item.iterdir()):
                if not test_item.is_dir() or not test_item.name.isdigit():
                    continue
                test_number = test_item.name
                folder_path = test_item
                yield run_id, config_name, test_number, folder_path
