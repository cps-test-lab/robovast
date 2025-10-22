import argparse
import os
import sys

from robovast_common import generate_floorplan_variations


def progress_callback(message):
    """Callback function to print progress updates."""
    print(message)


def main():
    parser = argparse.ArgumentParser(
        description='Generate floorplan variations and artifacts.',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument('--num-variations', "-n", type=int, required=True,
                        help='Number of variations to generate')
    parser.add_argument('--seed', "-s", type=int, required=True,
                        help='Seed forwarded to the floorplan variation generator')
    parser.add_argument('--floorplan-variation-files', "-f", type=str, nargs='+', required=True,
                        help='List of floorplan variation files to process')
    parser.add_argument('--output-dir', "-o", type=str, required=True,
                        help='Output directory for generated floorplans')
    parser.add_argument('--cache-dir', "-c", type=str,
                        help='Cache directory for intermediate files')

    args = parser.parse_args()

    num_variations = args.num_variations
    seed = args.seed
    variation_files = args.floorplan_variation_files

    cache_dir = args.cache_dir
    if not cache_dir:
        cache_dir = os.path.join(os.getcwd())

    if not os.path.exists(cache_dir):
        print(f"Error: Cache directory not found: {cache_dir}")
        return 1

    # Validate that all variation files exist
    rel_variation_files = []
    for vf in variation_files:
        if not os.path.exists(vf):
            print(f"Error: Variation file not found: {vf}")
            return 1
        rel_variation_files.append(os.path.relpath(vf, start=cache_dir))  # function expects relative paths

    print(f"Generating {num_variations} floorplan variations...")
    print(f"Using seed: {seed}")
    print(f"Variation files: {variation_files}")
    print(f"Output directory: {args.output_dir}")
    print(f"Cache directory: {cache_dir}")
    print("-" * 60)

    try:
        floorplan_dirs = generate_floorplan_variations(
            base_path=cache_dir,
            variation_files=rel_variation_files,
            num_variations=num_variations,
            seed_value=seed,
            output_dir=args.output_dir,
            progress_update_callback=progress_callback
        )

        if floorplan_dirs:
            print("-" * 60)
            print(f"✓ Successfully generated floorplan variations!")
            print(f"Output directories: {floorplan_dirs}")
            return 0
        else:
            print("✗ Failed to generate floorplan variations")
            return 1

    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
