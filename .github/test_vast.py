#!/usr/bin/env python3
"""Generic test script for VAST files - tests execution and postprocessing."""

import os
import argparse
import shutil
import subprocess
import sys
import tempfile
import signal
from pathlib import Path

import traceback

# Global variable to track the current subprocess
_current_process = None

def _signal_handler(signum, frame):
    """Handle signals and forward them to the subprocess."""
    global _current_process
    if _current_process is not None:
        print(f"\nReceived signal {signum}, forwarding to subprocess...")
        try:
            _current_process.send_signal(signum)
        except ProcessLookupError:
            # Process already terminated
            pass

def run_command(cmd, cwd=None, check=True):
    """Run a command and return the result."""
    global _current_process
    
    print(f"Running: {' '.join(cmd)}")
    
    # Set up signal handler
    old_sigint_handler = signal.signal(signal.SIGINT, _signal_handler)
    old_sigterm_handler = signal.signal(signal.SIGTERM, _signal_handler)
    
    try:
        _current_process = subprocess.Popen(
            cmd,
            cwd=cwd
        )
        
        # Wait for process to complete
        returncode = _current_process.wait()
        
        _current_process = None
        
        # Restore old signal handlers
        signal.signal(signal.SIGINT, old_sigint_handler)
        signal.signal(signal.SIGTERM, old_sigterm_handler)
        
        if check and returncode != 0:
            # Create a result-like object for compatibility
            class Result:
                def __init__(self, returncode):
                    self.returncode = returncode
            raise subprocess.CalledProcessError(returncode, cmd)
        
        # Return a result-like object
        class Result:
            def __init__(self, returncode):
                self.returncode = returncode
        
        return Result(returncode)
    
    except KeyboardInterrupt:
        # Handle Ctrl+C gracefully
        print("\nInterrupted by user")
        if _current_process is not None:
            try:
                _current_process.send_signal(signal.SIGINT)
                _current_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print("Process did not terminate, forcing kill...")
                _current_process.kill()
                _current_process.wait()
            except ProcessLookupError:
                pass
        _current_process = None
        signal.signal(signal.SIGINT, old_sigint_handler)
        signal.signal(signal.SIGTERM, old_sigterm_handler)
        raise
    
    finally:
        # Ensure we restore signal handlers
        try:
            signal.signal(signal.SIGINT, old_sigint_handler)
            signal.signal(signal.SIGTERM, old_sigterm_handler)
        except:
            pass


def check_results_dir_structure(results_dir):
    """Check that the results directory has the expected structure."""
    output_path = Path(results_dir)
    
    if not output_path.exists():
        print(f"✗ Results directory does not exist: {results_dir}")
        return False
    
    print(f"✓ Results directory exists: {results_dir}")
    
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

    # Check for scenario.osc file
    scenario_osc = [f for f in run_contents if f.name == 'scenario.osc']
    if not scenario_osc:
        print("  ✗ scenario.osc file not found in run directory")
        return False
    
    print("  ✓ scenario.osc file exists")
    
    # Check for execution.yaml file
    execution_yaml = [f for f in run_contents if f.name == 'execution.yaml']
    if not execution_yaml:
        print("  ✗ execution.yaml file not found in run directory")
        return False
    
    print("  ✓ execution.yaml file exists")
    
    # Check for config directories
    config_dirs = [d for d in run_contents if d.is_dir()]
    if not config_dirs:
        print("  ✗ No config directories found in run")
        return False
    
    print(f"  ✓ Found {len(config_dirs)} config directory/directories")
    
    # Check for _config directory
    config_dir = [d for d in config_dirs if d.is_dir() and d.name == '_config']
    if not config_dir:
        print("  ✗ _config directory not found in run")
        return False
    
    print("  ✓ _config directory exists")
    
    # Check for configurations.yaml in _config
    config_dir_path = config_dir[0]
    config_dir_contents = list(config_dir_path.iterdir())
    configurations_yaml = [f for f in config_dir_contents if f.name == 'configurations.yaml']
    if not configurations_yaml:
        print("  ✗ configurations.yaml file not found in _config directory")
        return False
    
    print("  ✓ configurations.yaml file exists in _config directory")
    
    # Check structure of first config
    first_config = config_dirs[0]
    config_contents = list(first_config.iterdir())
    print(f"    {first_config.name} contents: {[c.name for c in config_contents]}")
    
    # Check that only "_config" and numbers exist as directory names
    for item in config_contents:
        if item.is_dir():
            name = item.name
            if not (name == "_config" or name.isdigit()):
                print(f"    ✗ Invalid directory name: {name} (expected '_config' or numeric)")
                return False
            
            if name.isdigit():
                # Check for test.xml file in numeric directories
                numeric_dir_contents = list(item.iterdir())
                test_xml = [f for f in numeric_dir_contents if f.name == 'test.xml']
                if not test_xml:
                    print(f"    ✗ test.xml file not found in {name} directory")
                    return False
    
    print("    ✓ Directory names are valid (_config and/or numbers)")
    print("    ✓ test.xml files exist in numeric directories")
    
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


def test_vast_workflow(vast_file_path, config=None):  # pylint: disable=too-many-return-statements
    """Test complete VAST workflow: init -> execution -> postprocessing."""
    print("\n" + "="*60)
    print("Testing: Complete VAST workflow")
    print("="*60)
    
    # Get the config path and repo root
    repo_root = Path(__file__).parent.parent
    config_path = Path(vast_file_path)
    
    # Handle relative paths
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    
    if not config_path.exists():
        print(f"✗ Config file not found: {config_path}")
        return False
    
    print(f"✓ Config file found: {config_path}")
    
    try:
            
        with tempfile.TemporaryDirectory() as temp_output:
            # Step 1: vast init
            print("\n--- Step 1: vast init ---")
            cmd_init = [
                'poetry', 'run', '--directory', str(repo_root),
                'vast', 'init', str(config_path)
            ]
            
            result = run_command(cmd_init, cwd=temp_output)
            
            if result.returncode != 0:
                print("✗ vast init failed")
                return False
            
            print("✓ vast init executed successfully")
            
            # Check for .robovast_project file (critical for execution step)
            if not os.path.exists(os.path.join(temp_output, '.robovast_project')):
                print("✗ .robovast_project file not found - execution step will fail")
                return False
            
            print("✓ .robovast_project file exists - environment is properly initialized")
        
            # Step 2: vast execution local run
            # Use a temporary directory for output
            print("\n--- Step 2: vast execution local run ---")
            
            cmd_exec = [
                'poetry', 'run', '--directory', str(repo_root),
                'vast', 'execution', 'local', 'run', "-r", "1"
            ]
            
            # Add config option if provided
            if config:
                cmd_exec.extend(["-c", config])
            
            result = run_command(cmd_exec, cwd=temp_output)
            
            if result.returncode != 0:
                print("✗ vast execution local run failed")
                return False
            
            print("✓ vast execution local run executed successfully")
            
            # Check output structure
            if not check_results_dir_structure(os.path.join(temp_output, "results")):
                return False
            
            print("✓ Output structure is valid")
            
            # Step 3: vast analysis postprocess
            print("\n--- Step 3: vast analysis postprocess ---")
                        
            cmd_postprocess = [
                'poetry', 'run', '--directory', str(repo_root),
                'vast', 'analysis', 'postprocess'
            ]
            
            # Execute in the repo root where .robovast_project exists
            result = run_command(cmd_postprocess, cwd=temp_output)
            
            if result.returncode != 0:
                print("✗ vast analysis postprocess failed")
                return False
            
            print("✓ vast analysis postprocess executed successfully")
            
            # Check for .postprocessed file
            if not check_postprocessed_file(os.path.join(temp_output, "results")):
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
        help='Configuration to run (will be passed as -c <config> to vast execution)'
    )
    
    args = parser.parse_args()
    
    print("="*60)
    print("VAST Workflow Tests")
    print("="*60)
    print(f"VAST file: {args.vast_file}")
    if args.config:
        print(f"Configuration: {args.config}")
    
    tests = [
        ("Complete workflow: init -> execution -> postprocess", test_vast_workflow, args.vast_file, args.config),
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
