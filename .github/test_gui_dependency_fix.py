#!/usr/bin/env python3
"""
Test to verify the fix for GUI dependency issue in headless environments.

This script reproduces and tests the fix for the issue where variation plugins
failed to load in GitHub Actions due to missing graphics libraries (libEGL.so.1).

The issue occurred because:
1. Variation plugins are loaded via entry points when using the CLI
2. The variation module's __init__.py imported GUI classes at the top level
3. GUI classes import PySide6, which requires graphics libraries
4. GitHub Actions runners don't have these graphics libraries installed
5. This caused all variation plugins to fail loading with "libEGL.so.1: cannot open shared object file"

The fix:
- Made GUI imports lazy using __getattr__ in variation/__init__.py
- GUI classes are now only imported when actually needed
- CLI operations don't trigger GUI imports anymore
"""

import sys
import os

def test_variation_imports():
    """Test that variation classes can be imported without triggering GUI imports."""
    print("Testing variation imports in headless environment...")
    
    # Simulate headless environment by checking if we can import variation classes
    # without triggering PySide6 imports
    try:
        # This should work even without graphics libraries
        from robovast.common.variation import (
            ParameterVariationList,
            ParameterVariationDistributionUniform,
            ParameterVariationDistributionGaussian
        )
        print("✓ Successfully imported variation classes")
        print(f"  - ParameterVariationList: {ParameterVariationList}")
        print(f"  - ParameterVariationDistributionUniform: {ParameterVariationDistributionUniform}")
        print(f"  - ParameterVariationDistributionGaussian: {ParameterVariationDistributionGaussian}")
        return True
    except ImportError as e:
        if 'libEGL' in str(e) or 'Qt' in str(e):
            print(f"✗ FAILED: GUI dependencies still being imported: {e}")
            print("  This means the lazy import fix didn't work correctly")
            return False
        else:
            print(f"✗ FAILED: Different import error: {e}")
            return False
    except Exception as e:
        print(f"✗ FAILED: Unexpected error: {e}")
        return False


def test_gui_import_is_lazy():
    """Test that GUI classes are only imported when accessed."""
    print("\nTesting lazy GUI import...")
    
    try:
        # Import the module but don't access GUI classes
        import robovast.common.variation
        print("✓ Module imported without triggering GUI imports")
        
        # Check if PySide6 is in sys.modules (it shouldn't be yet)
        if 'PySide6' in sys.modules or any('PySide6' in m for m in sys.modules):
            print("✗ WARNING: PySide6 was imported during module import")
            print("  This means GUI imports are not fully lazy")
            return False
        else:
            print("✓ PySide6 not imported yet (GUI imports are lazy)")
            return True
            
    except Exception as e:
        print(f"✗ FAILED: {e}")
        return False


def test_entry_point_loading():
    """Test that entry points can load without errors."""
    print("\nTesting entry point loading...")
    
    try:
        from importlib.metadata import entry_points
        
        eps = entry_points()
        variation_eps = list(eps.select(group='robovast.variation_types'))
        
        if not variation_eps:
            print("✗ FAILED: No variation entry points found")
            return False
        
        print(f"✓ Found {len(variation_eps)} variation entry points")
        
        # Try to load each one
        failed = []
        for ep in variation_eps:
            try:
                cls = ep.load()
                print(f"  ✓ {ep.name}: loaded successfully")
            except ImportError as e:
                if 'libEGL' in str(e) or 'Qt' in str(e):
                    print(f"  ✗ {ep.name}: GUI dependency error - {e}")
                    failed.append(ep.name)
                else:
                    print(f"  ✗ {ep.name}: import error - {e}")
                    failed.append(ep.name)
            except Exception as e:
                print(f"  ✗ {ep.name}: unexpected error - {e}")
                failed.append(ep.name)
        
        if failed:
            print(f"\n✗ FAILED: {len(failed)} entry points failed to load")
            return False
        else:
            print("\n✓ All entry points loaded successfully")
            return True
            
    except Exception as e:
        print(f"✗ FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    print("="*60)
    print("Testing GUI Dependency Fix for Headless Environments")
    print("="*60)
    print()
    
    results = []
    
    # Run tests
    results.append(("Variation imports", test_variation_imports()))
    results.append(("Lazy GUI imports", test_gui_import_is_lazy()))
    results.append(("Entry point loading", test_entry_point_loading()))
    
    # Print summary
    print()
    print("="*60)
    print("Test Results Summary")
    print("="*60)
    
    all_passed = True
    for name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {name}")
        if not passed:
            all_passed = False
    
    print("="*60)
    
    if all_passed:
        print("✓ All tests passed! The fix works correctly.")
        return 0
    else:
        print("✗ Some tests failed. The issue may not be fully resolved.")
        return 1


if __name__ == '__main__':
    sys.exit(main())
