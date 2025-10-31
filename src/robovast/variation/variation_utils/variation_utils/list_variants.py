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
import os
import sys
import tempfile

from robovast.common import generate_scenario_variations


def progress_callback(message):
    """Callback function to print progress updates."""
    print(message)

def main():  # pylint: disable=too-many-return-statements

    parser = argparse.ArgumentParser(
        description='List scenario variants.',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # Mode 1: Using scenario file
    parser.add_argument('--config', type=str, required=True,
                        help='Path to .vast configuration file')

    args = parser.parse_args()

    print(f"Listing scenario variants from {args.config}...")
    print("-" * 60)

    temp_path = tempfile.TemporaryDirectory(prefix="list_variants_")
    try:
        variants = generate_scenario_variations(
            variation_file=args.config,
            progress_update_callback=progress_callback,
            output_dir=temp_path.name
        )

        if variants:
            print("-" * 60)
            variants_file = os.path.join(temp_path.name, "scenario.variants")
            if os.path.exists(variants_file):
                with open(variants_file, "r", encoding="utf-8") as vf:
                    print(vf.read())
            else:
                print(f"No scenario.variants file found at {variants_file}")
            return 0
        else:
            print("✗ Failed to list scenario variants")
            return 1

    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
