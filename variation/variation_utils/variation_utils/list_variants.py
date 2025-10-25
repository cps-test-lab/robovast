import argparse
import os
import sys
import tempfile
from robovast_common import generate_scenario_variations


def progress_callback(message):
    """Callback function to print progress updates."""
    print(message)


def main():  # pylint: disable=too-many-return-statements

    parser = argparse.ArgumentParser(
        description='List scenario variants.',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # Mode 1: Using scenario file
    parser.add_argument('--config', type=str, 
                        help='config file specifying all parameters')

    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"Error: config file not found: {args.config}")
        return 1

    print(f"Listing Generating scenario variants from {args.config}...")
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
