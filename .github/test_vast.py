#!/usr/bin/env python3
"""Generic test script for VAST files - tests execution and postprocessing."""

import argparse
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path


def run_command(cmd, repo_root, cwd=None, check=True, stream_output=False):
    """Run a command, stream its output, and return the exit code.

    On failure always prints clearly labelled stdout and stderr so CI logs
    contain enough context to debug the problem.
    """
    print(f"Running: {cmd}")

    if stream_output:
        # Stream output directly to stdout for live visibility
        result = subprocess.run(
            ['poetry', 'run', '--directory', str(repo_root),
            'bash', '-c', f'cd {cwd} && {cmd}'],
            text=True,
            check=False,
        )
    else:
        result = subprocess.run(
            ['poetry', 'run', '--directory', str(repo_root),
            'bash', '-c', f'cd {cwd} && {cmd}'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        # Always emit stdout so progress is visible in the CI log.
        if result.stdout:
            sys.stdout.write(result.stdout)
            sys.stdout.flush()

    if result.returncode != 0:
        print(f"\n✗ Command exited with code {result.returncode}")
        if hasattr(result, 'stdout') and result.stdout:
            print("--- stdout ---")
            print(result.stdout)
        if hasattr(result, 'stderr') and result.stderr:
            print("--- stderr ---")
            print(result.stderr)
        if check:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, getattr(result, 'stdout', ''), getattr(result, 'stderr', '')
            )

    return result.returncode


def check_results_dir_structure(results_dir):  # pylint: disable=too-many-return-statements
    """Check that the results directory has the expected structure."""
    output_path = Path(results_dir)
    
    if not output_path.exists():
        print(f"✗ Results directory does not exist: {results_dir}")
        return False
    
    print(f"✓ Results directory exists: {results_dir}")
    
    # List contents
    contents = list(output_path.iterdir())
    print(f"  Contents: {[c.name for c in contents]}")
    
    # Check for campaign directories (prefer campaign-*, but accept legacy run-*)
    campaign_dirs = [
        d for d in contents
        if d.is_dir() and (d.name.startswith('campaign-') or d.name.startswith('run-'))
    ]
    if not campaign_dirs:
        print("✗ No campaign or run directories found (expected campaign-* or run-* directories)")
        return False
    
    print(f"✓ Found {len(campaign_dirs)} campaign directory/directories")
    
    # Check structure of first campaign directory
    first_run = campaign_dirs[0]
    print(f"  Checking structure of {first_run.name}:")
    
    # Look for expected files/directories in campaign directory
    campaign_contents = list(first_run.iterdir())
    print(f"    Contents: {[c.name for c in campaign_contents]}")

    # Check for scenario.osc file in _config/
    config_dir_check = first_run / '_config'
    if config_dir_check.exists():
        scenario_osc = config_dir_check / 'scenario.osc'
        if not scenario_osc.exists():
            print("  ✗ scenario.osc file not found in _config/ directory")
            return False
        print("  ✓ scenario.osc file exists in _config/")
    else:
        print("  ✗ _config directory not found in campaign directory")
        return False
    
    # Check for execution.yaml file in _execution/
    execution_dir = first_run / '_execution'
    if not execution_dir.exists() or not (execution_dir / 'execution.yaml').exists():
        print("  ✗ execution.yaml file not found in _execution/ directory")
        return False

    print("  ✓ execution.yaml file exists in _execution/")
    
    # Check for config directories
    config_dirs = [d for d in campaign_contents if d.is_dir()]
    if not config_dirs:
        print("  ✗ No config directories found in campaign")
        return False
    
    print(f"  ✓ Found {len(config_dirs)} config directory/directories")
    
    # Check for _config directory
    config_dir = [d for d in config_dirs if d.is_dir() and d.name == '_config']
    if not config_dir:
        print("  ✗ _config directory not found in campaign")
        return False
    
    print("  ✓ _config directory exists")
    
    # Check structure of first scenario directory (exclude _config; it has its own layout)
    scenario_dirs = [d for d in config_dirs if d.name not in ('_config', '_transient', '_execution')]
    if not scenario_dirs:
        print("  ✗ No scenario directory found in run")
        return False
    first_scenario = scenario_dirs[0]
    config_contents = list(first_scenario.iterdir())
    

    transient_dir = [d for d in config_dirs if d.name == '_transient']
    if not transient_dir:
        print("  ✗ _transient directory not found in run")
        return False
    
    print("  ✓ _transient directory exists")
    
    # Check for configurations.yaml in _transient directory
    transient_dir_path = transient_dir[0]
    transient_dir_contents = list(transient_dir_path.iterdir())
    configurations_yaml = [f for f in transient_dir_contents if f.name == 'configurations.yaml']
    if not configurations_yaml:
        print("  ✗ configurations.yaml file not found in _transient directory")
        return False
    
    print("  ✓ configurations.yaml file exists in _transient directory")

    run_dirs = [d for d in config_contents if d.name not in ('_config')]
    print(f"    {first_scenario.name} contents: {[c.name for c in run_dirs]}")
    
    # Check that only numeric directories exist (and require test.xml in each)
    for item in run_dirs:
        if item.is_dir():
            name = item.name
            if not name.isdigit():
                print(f"    ✗ Invalid directory name: {name} (expected numeric run index)")
                return False
            
            if name.isdigit():
                # Check for test.xml file in numeric directories
                numeric_dir_contents = list(item.iterdir())
                test_xml = [f for f in numeric_dir_contents if f.name == 'test.xml']
                if not test_xml:
                    print(f"    ✗ test.xml file not found in {name} directory")
                    return False
    
    print("    ✓ Directory names are valid (numeric run indices)")
    print("    ✓ test.xml files exist in numeric directories")
    
    return True


def test_vast_workflow(vast_file_path, test_directory, config=None, runs=None):  # pylint: disable=too-many-return-statements
    """Test complete VAST workflow: init -> execution -> postprocessing."""
    print("\n" + "="*60)
    print("Testing: Complete VAST workflow")
    print("="*60)
    print(f"Test directory: {test_directory}")
    print(f"VAST file: {vast_file_path}")
    
    # Get the config path and repo root
    repo_root = Path(__file__).parent.parent
    config_path = Path(vast_file_path)
    results_dir = os.path.join(test_directory, "results")
    
    # Handle relative paths
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    
    if not config_path.exists():
        print(f"✗ Config file not found: {config_path}")
        return False
    
    print(f"✓ Config file found: {config_path}")
    
    try:
        # Step 1: vast init
        print("\n--- Step 1: vast init ---")
        cmd_init = f"vast init {config_path}"
        
        result = run_command(cmd_init, repo_root, cwd=test_directory)
        
        if result != 0:
            print("✗ vast init failed")
            return False
        
        print("✓ vast init executed successfully")
        
        # Check for .robovast_project file (critical for execution step)
        if not os.path.exists(os.path.join(repo_root, '.robovast_project')):
            print("✗ .robovast_project file not found - execution step will fail")
            return False
        
        print("✓ .robovast_project file exists - environment is properly initialized")
    
        # Step 2: vast exec local run
        # Use a temporary directory for output
        print("\n--- Step 2: vast exec local run ---")
        
        cmd_exec = f'vast exec local run'
        if runs:
            cmd_exec += f' -r {runs}'
        
        # Add config option if provided
        if config:
            cmd_exec += f' -c {config}'
        
        result = run_command(cmd_exec, repo_root, cwd=test_directory, stream_output=True)
        
        if result != 0:
            print("✗ vast exec local run failed")
            return False
        
        print("✓ vast exec local run executed successfully")
        
        # Check output structure
        if not check_results_dir_structure(results_dir):
            return False
        
        print("✓ Output structure is valid")
        
        # Step 3: vast results postprocess
        print("\n--- Step 3: vast results postprocess ---")

        cmd_postprocess = f'vast results postprocess'

        # Execute in the repo root where .robovast_project exists
        result = run_command(cmd_postprocess, repo_root, cwd=repo_root)

        if result != 0:
            print("\u2717 vast results postprocess failed")
            return False

        print("\u2713 vast results postprocess executed successfully")
        
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
    parser = argparse.ArgumentParser(
        description='Test VAST file workflow: init -> execution -> postprocessing'
    )
    parser.add_argument(
        'vast_file',
        type=str,
        help='Path to the VAST configuration file (absolute or relative to repo root)'
    )
    parser.add_argument(
        '-c', '--config',
        type=str,
        default=None,
        help='Configuration to run (will be passed as -c <config> to vast exec)'
    )
    parser.add_argument(
        '-d', '--test-directory',
        type=str,
        required=True,
        help='Directory for test output (will be passed as -d <directory> to vast exec)'
    )
    parser.add_argument(
        '-r', '--runs',
        type=int,
        default=None,
        help='Number of runs (will be passed as -r <runs> to vast exec)'
    )
    
    args = parser.parse_args()
    
    print("="*60)
    print("VAST Workflow Tests")
    print("="*60)
    print(f"VAST file: {args.vast_file}")
    if args.config:
        print(f"Configuration: {args.config}")
    if args.test_directory:
        print(f"Test directory: {args.test_directory}")
    
    tests = [
        ("Complete workflow: init -> execution -> postprocess", test_vast_workflow, args.vast_file, args.test_directory, args.config, args.runs),
    ]
    
    results = []
    for name, test_func, *test_args in tests:
        try:
            result = test_func(*test_args)
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
