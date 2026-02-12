#!/usr/bin/env python3
"""Diagnostic script to check if robovast plugins are properly registered."""

import sys
from importlib.metadata import entry_points, version, distributions

def main():
    print("="*60)
    print("RoboVAST Plugin Diagnostics")
    print("="*60)
    
    # Check if robovast is installed
    print("\n1. Checking robovast installation...")
    try:
        robovast_version = version('robovast')
        print(f"   ✓ robovast is installed: version {robovast_version}")
    except Exception as e:
        print(f"   ✗ robovast is NOT installed or not found: {e}")
        print("   This is the root cause - run 'poetry install' to install the package")
        return 1
    
    # Check installed distributions
    print("\n2. Checking installed distributions...")
    try:
        dists = list(distributions())
        robovast_dists = [d for d in dists if 'robovast' in d.name.lower()]
        if robovast_dists:
            for dist in robovast_dists:
                print(f"   ✓ Found: {dist.name} {dist.version}")
        else:
            print("   ⚠ No robovast distributions found")
    except Exception as e:
        print(f"   ⚠ Could not check distributions: {e}")
    
    # Check entry points
    print("\n3. Checking entry points...")
    try:
        eps = entry_points()
        print(f"   Entry points object: {type(eps)}")
        
        # Check robovast.variation_types group
        print("\n4. Checking 'robovast.variation_types' entry points...")
        try:
            variation_eps = eps.select(group='robovast.variation_types')
            variation_list = list(variation_eps)
            
            if variation_list:
                print(f"   ✓ Found {len(variation_list)} variation type(s):")
                for ep in variation_list:
                    print(f"      - {ep.name}: {ep.value}")
                    try:
                        cls = ep.load()
                        print(f"        ✓ Successfully loaded: {cls}")
                    except Exception as e:
                        print(f"        ✗ Failed to load: {e}")
            else:
                print("   ✗ No variation types found!")
                print("   This means entry points are not registered.")
                print("   Possible causes:")
                print("      - Package not installed (run 'poetry install')")
                print("      - Package installed with --no-root flag")
                print("      - Package metadata not built properly")
                return 1
        except Exception as e:
            print(f"   ✗ Failed to select variation_types group: {e}")
            return 1
            
        # Check other robovast entry point groups
        print("\n5. Checking other robovast entry point groups...")
        for group_name in ['robovast.cluster_configs', 'robovast.postprocessing_commands']:
            try:
                group_eps = eps.select(group=group_name)
                group_list = list(group_eps)
                if group_list:
                    print(f"   ✓ {group_name}: {len(group_list)} entry point(s)")
                else:
                    print(f"   ⚠ {group_name}: No entry points found")
            except Exception as e:
                print(f"   ⚠ {group_name}: Error checking - {e}")
                
    except Exception as e:
        print(f"   ✗ Failed to load entry points: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    print("\n" + "="*60)
    print("✓ All checks passed! Plugins are properly registered.")
    print("="*60)
    return 0

if __name__ == '__main__':
    sys.exit(main())
