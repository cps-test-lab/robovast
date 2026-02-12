#!/usr/bin/env python3
"""Test script for growth_sim example - tests execution and postprocessing."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import traceback

def run_command(cmd, cwd=None, check=True):
    """Run a command and return the result."""
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    
    return result


def check_output_structure(output_dir):
    """Check that the output directory has the expected structure."""
    output_path = Path(output_dir)
    
    if not output_path.exists():
        print(f"✗ Output directory does not exist: {output_dir}")
        return False
    
    print(f"✓ Output directory exists: {output_dir}")
    
    # List contents
    contents = list(output_path.iterdir())
    print(f"  Contents: {[c.name for c in contents]}")
    
    # Check for run directories (run-*)
    run_dirs = [d for d in contents if d.is_dir() and d.name.startswith('run-')]
    if not run_dirs:
        print("✗ No run directories found (expected run-* directories)")
        return False
    
    print(f"✓ Found {len(run_dirs)} run directory/directories")
    
    # Check structure of first run directory
    first_run = run_dirs[0]
    print(f"  Checking structure of {first_run.name}:")
    
    # Look for expected files/directories in run
    run_contents = list(first_run.iterdir())
    print(f"    Contents: {[c.name for c in run_contents]}")
    
    # Check for config directories
    config_dirs = [d for d in run_contents if d.is_dir()]
    if not config_dirs:
        print("  ✗ No config directories found in run")
        return False
    
    print(f"  ✓ Found {len(config_dirs)} config directory/directories")
    
    # Check structure of first config
    first_config = config_dirs[0]
    config_contents = list(first_config.iterdir())
    print(f"    {first_config.name} contents: {[c.name for c in config_contents]}")
    
    # Check for test directories
    test_dirs = [d for d in config_contents if d.is_dir() and d.name.startswith('test')]
    if not test_dirs:
        print("    ⚠ Warning: No test directories found")
    else:
        print(f"    ✓ Found {len(test_dirs)} test directory/directories")
    
    return True


def check_postprocessed_file(results_dir):
    """Check that the .postprocessed file exists in the results directory."""
    postprocessed_file = Path(results_dir) / '.postprocessed'
    
    if not postprocessed_file.exists():
        print(f"✗ .postprocessed file not found: {postprocessed_file}")
        return False
    
    print(f"✓ .postprocessed file exists: {postprocessed_file}")
    
    # Read and display contents
    try:
        with open(postprocessed_file, 'r') as f:
            content = f.read().strip()
        print(f"  Content: {content}")
    except Exception as e:
        print(f"  Warning: Could not read file: {e}")
    
    return True


def test_growth_sim_workflow():  # pylint: disable=too-many-return-statements
    """Test complete growth_sim workflow: init -> execution -> postprocessing."""
    print("\n" + "="*60)
    print("Testing: Complete growth_sim workflow")
    print("="*60)
    
    # Get the growth_sim config path and repo root
    repo_root = Path(__file__).parent.parent
    config_path = repo_root / 'configs' / 'examples' / 'growth_sim' / 'growth_sim.vast'
    
    if not config_path.exists():
        print(f"✗ Config file not found: {config_path}")
        return False
    
    print(f"✓ Config file found: {config_path}")
    
    # vast init creates project in repo root, so we work there instead of temp directory
    # Save and restore the .robovast_project file if it exists
    robovast_project = repo_root / '.robovast_project'
    backup_path = None
    if robovast_project.exists():
        backup_path = repo_root / '.robovast_project.backup'
        shutil.copy(robovast_project, backup_path)
        print(f"  Backed up existing .robovast_project to {backup_path.name}")
    
    try:
        # Step 1: vast init
        print("\n--- Step 1: vast init ---")
        cmd_init = [
            'poetry', 'run', '--directory', str(repo_root),
            'vast', 'init', str(config_path)
        ]
        
        result = run_command(cmd_init, cwd=repo_root)
        
        if result.returncode != 0:
            print("✗ vast init failed")
            return False
        
        print("✓ vast init executed successfully")
        
        # Check for .robovast_project file (critical for execution step)
        if not robovast_project.exists():
            print("✗ .robovast_project file not found - execution step will fail")
            return False
        
        print("✓ .robovast_project file exists - environment is properly initialized")
            
            # Check for expected files
            expected_items = ['configs', 'growth_sim.vast', '.cache']
            found_items = [f.name for f in created_files]
            
            has_expected = any(item in found_items for item in expected_items)
            if has_expected:
                print(f"✓ Found expected items: {[item for item in expected_items if item in found_items]}")
            else:
                print(f"⚠ Warning: Expected items not found. Created: {found_items}")
            
            # Step 2: vast execution local run
            print("\n--- Step 2: vast execution local run ---")
            output_dir_name = 'test_output'
            
            cmd_exec = [
                'poetry', 'run', '--directory', str(repo_root),
                'vast', 'execution', 'local', 'run',
                '--config', 'test-fixed-values',
                '--runs', '1',
                '--output', output_dir_name
            ]
            
            result = run_command(cmd_exec, cwd=temp_path)
            
            if result.returncode != 0:
                print("✗ vast execution local run failed")
                return False
            
            print("✓ vast execution local run executed successfully")
            
            # Check output structure
            output_dir = temp_path / output_dir_name
            if not check_output_structure(output_dir):
                return False
            
            print("✓ Output structure is valid")
            
            # Step 3: vast analysis preprocess
            print("\n--- Step 3: vast analysis postprocess ---")
            results_dir = output_dir
            
            # Copy the config file to the results directory as expected by postprocess
            shutil.copy(config_path, results_dir / 'growth_sim.vast')
            
            cmd_postprocess = [
                'poetry', 'run', '--directory', str(repo_root),
                'vast', 'analysis', 'postprocess',
                '--results-dir', str(results_dir)
            ]
            
            # Execute in the same directory where vast init was called (where .robovast_project exists)
            result = run_command(cmd_postprocess, cwd=temp_path)
            
            if result.returncode != 0:
                print("✗ vast analysis postprocess failed")
                return False
            
            print("✓ vast analysis postprocess executed successfully")
            
            # Check for .postprocessed file
            if not check_postprocessed_file(results_dir):
                return False
            
            print("✓ Postprocessing completed successfully")
            
            print("\n✓ Complete workflow succeeded!")
            return True
            
        except subprocess.CalledProcessError as e:
            print(f"✗ Command failed with exit code {e.returncode}")
            return False
        except Exception as e:
            print(f"✗ Unexpected error: {e}")
            traceback.print_exc()
            return False


def main():
    """Run all tests."""
    print("="*60)
    print("Growth Sim Example Tests")
    print("="*60)
    
    tests = [
        ("Complete workflow: init -> execution -> postprocess", test_growth_sim_workflow),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"✗ Test '{name}' raised exception: {e}")
            traceback.print_exc()
            results.append((name, False))
    
    # Print summary
    print("\n" + "="*60)
    print("Test Results Summary")
    print("="*60)
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {name}")
    
    all_passed = all(result for _, result in results)
    print("="*60)
    if all_passed:
        print("✓ All tests passed!")
        return 0
    else:
        print("✗ Some tests failed!")
        return 1


if __name__ == '__main__':
    sys.exit(main())
