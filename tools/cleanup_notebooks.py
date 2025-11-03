#!/usr/bin/env python3
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

import argparse
import re
import sys
from pathlib import Path

try:
    import nbformat
    from nbconvert.preprocessors import ClearOutputPreprocessor
except ImportError:
    print("Error: Required packages not found. Please install:")
    print("  pip install nbformat nbconvert")
    sys.exit(1)


def cleanup_notebook(notebook_path: Path, reset_data_dir: bool = True) -> bool:
    try:
        print(f"Cleaning notebook: {notebook_path}")

        # Read the notebook
        with open(notebook_path, 'r', encoding='utf-8') as f:
            notebook = nbformat.read(f, as_version=4)

        # Clear outputs
        clear_output = ClearOutputPreprocessor()
        notebook, _ = clear_output.preprocess(notebook, {})

        # Reset DATA_DIR if requested
        if reset_data_dir:
            regex_pattern = re.compile(r"(?m)^(\s*)DATA_DIR\s*=\s*['\"].*?['\"](.*)$")

            for cell in notebook.cells:
                if cell.cell_type == 'code':
                    # Replace DATA_DIR assignment with empty string
                    cell.source = regex_pattern.sub(
                        r"\1DATA_DIR = ''\2",
                        cell.source
                    )

        # Write back the cleaned notebook
        with open(notebook_path, 'w', encoding='utf-8') as f:
            nbformat.write(notebook, f)

        return True

    except Exception as e:
        print(f"Error cleaning {notebook_path}: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Clean Jupyter notebooks by clearing outputs and resetting DATA_DIR"
    )
    parser.add_argument(
        'path',
        nargs='?',
        default='.',
        help='Directory to search for notebooks (default: current directory)'
    )
    parser.add_argument(
        '--no-reset-data-dir',
        action='store_true',
        help='Do not reset DATA_DIR variable'
    )

    args = parser.parse_args()

    search_path = Path(args.path).resolve()

    if not search_path.exists():
        print(f"Error: Path does not exist: {search_path}", file=sys.stderr)
        return 1

    # Find all notebook files
    if search_path.is_file():
        notebook_files = [search_path]
    else:
        notebook_files = list(search_path.rglob('*.ipynb'))

    if not notebook_files:
        print(f"No notebook files found in {search_path}")
        return 0

    print(f"Found {len(notebook_files)} notebook(s) to clean")

    # Clean each notebook
    success_count = 0
    failure_count = 0

    for notebook_file in notebook_files:
        if cleanup_notebook(notebook_file, reset_data_dir=not args.no_reset_data_dir):
            success_count += 1
        else:
            failure_count += 1

    print(f"\nCleaned {success_count} notebook(s) successfully")
    if failure_count > 0:
        print(f"Failed to clean {failure_count} notebook(s)", file=sys.stderr)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
