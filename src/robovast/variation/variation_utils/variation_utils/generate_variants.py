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

from robovast.common import generate_scenario_variations

def progress_callback(message):
    """Callback function to print progress updates."""
    print(message)


def main():  # pylint: disable=too-many-return-statements

    parser = argparse.ArgumentParser(
        description='Generate test variants.',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # Mode 1: Using scenario file
    parser.add_argument('--config', type=str, required=True,
                        help='Path to .vast configuration file')

    # Common parameters
    parser.add_argument('--output', "-o", type=str, required=True,
                        help='Output directory for generated scenarios variants and files')

    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"Error: config file not found: {args.config}")
        return 1

    print(f"Generating scenario variants from {args.config}...")
    print(f"Output directory: {args.output}")
    print("-" * 60)

    try:
        variants = generate_scenario_variations(
            variation_file=args.config,
            progress_update_callback=progress_callback,
            output_dir=args.output
        )

        if variants:
            print("-" * 60)
            print(f"✓ Successfully generated {len(variants)} scenario variants!")
            return 0
        else:
            print("✗ Failed to generate scenario variants")
            return 1

    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
