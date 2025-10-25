import argparse
import os
import sys

from robovast_common import generate_scenario_variations


def progress_callback(message):
    """Callback function to print progress updates."""
    print(message)


def main():  # pylint: disable=too-many-return-statements

    parser = argparse.ArgumentParser(
        description='Generate scenario variants.',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # Mode 1: Using scenario file
    parser.add_argument('--config', type=str, 
                        help='config file specifying all parameters')

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
