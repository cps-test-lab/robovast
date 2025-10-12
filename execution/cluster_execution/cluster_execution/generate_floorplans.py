import argparse
import os
import yaml
from robovast_common import generate_floorplan_variations, get_scenario_base_path

def progress_callback(message):
    """Callback function to print progress updates."""
    print(message)

def load_scenario_config(scenario_file):
    """Load and parse scenario variation file."""
    with open(scenario_file, 'r') as f:
        # Load all documents, the first one contains the settings
        documents = list(yaml.safe_load_all(f))
        if not documents:
            raise ValueError("No documents found in scenario file")
        config = documents[0]
    
    floorplan_config = config.get('settings', {}).get('floorplan_variation', {})
    
    num_variations = floorplan_config.get('num_variations')
    seed = floorplan_config.get('floorplan_variation_seed')
    variation_files = floorplan_config.get('variation_files', [])
    
    return num_variations, seed, variation_files

def main():
    scenario_base_path = get_scenario_base_path()
    default_scenario_file = os.path.join(scenario_base_path, "scenario.variants")
    
    parser = argparse.ArgumentParser(
        description='Generate floorplan variations and artifacts. '
                    'Either specify --scenario-variation-file OR specify --num-variations, --seed, and --floorplan-variation-files.',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    # Mode 1: Using scenario file
    parser.add_argument('--scenario-variation-file', type=str, 
                        help='Scenario variation file specifying all parameters (num_variations, seed, variation_files)')
    
    # Mode 2: Direct parameters
    parser.add_argument('--num-variations', "-n", type=int, 
                        help='Number of variations to generate')
    parser.add_argument('--seed', "-s", type=int, 
                        help='Seed forwarded to the floorplan variation generator')
    parser.add_argument('--floorplan-variation-files', "-f", type=str, nargs='+',
                        help='List of floorplan variation files to process')
    
    # Common parameters
    parser.add_argument('--output-dir', "-o", type=str, required=True,
                        help='Output directory for generated floorplans')
    
    args = parser.parse_args()

    # Determine which mode we're in
    using_scenario_file = args.scenario_variation_file is not None
    using_direct_params = any([args.num_variations is not None, 
                                args.seed is not None, 
                                args.floorplan_variation_files is not None])
    
    # Validate mutual exclusivity
    if using_scenario_file and using_direct_params:
        print("Error: Cannot specify both --scenario-variation-file and direct parameters "
              "(--num-variations, --seed, --floorplan-variation-files)")
        return 1
    
    if not using_scenario_file and not using_direct_params:
        # Default to scenario file mode if nothing specified
        args.scenario_variation_file = default_scenario_file
        using_scenario_file = True
    
    # Mode 1: Load from scenario file
    if using_scenario_file:
        if not os.path.exists(args.scenario_variation_file):
            print(f"Error: Scenario variation file not found: {args.scenario_variation_file}")
            return 1
        
        try:
            num_variations, seed, variation_files = load_scenario_config(args.scenario_variation_file)
            print(f"Loaded configuration from: {args.scenario_variation_file}")
        except Exception as e:
            print(f"Error loading scenario file: {e}")
            return 1
        
        if not num_variations or not seed or not variation_files:
            print("Error: Scenario file must contain num_variations, floorplan_variation_seed, and variation_files")
            return 1
    
    # Mode 2: Use direct parameters
    else:
        if args.num_variations is None or args.seed is None or args.floorplan_variation_files is None:
            print("Error: When not using --scenario-variation-file, you must specify --num-variations, --seed, and --floorplan-variation-files")
            return 1
        
        num_variations = args.num_variations
        seed = args.seed
        variation_files = args.floorplan_variation_files
        
        # Validate that all variation files exist
        for vf in variation_files:
            if not os.path.exists(vf):
                print(f"Error: Variation file not found: {vf}")
                return 1
    
    print(f"Generating {num_variations} floorplan variations...")
    print(f"Using seed: {seed}")
    print(f"Variation files: {variation_files}")
    print(f"Output directory: {args.output_dir}")
    print("-" * 60)

    try:
        floorplan_dirs = generate_floorplan_variations(
            variation_files=variation_files,
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
        import traceback
        traceback.print_exc()
        return 1

if __name__ == '__main__':
    exit(main())
