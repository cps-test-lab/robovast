#!/usr/bin/env python3
"""Generic test script for VAST files - tests execution and postprocessing."""

import argparse
import math
import os
import subprocess
import sys
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
        if d.is_dir() and (d.name.startswith('campaign-') or d.name.startswith('growth_sim-'))
    ]
    if not campaign_dirs:
        print("✗ No campaign or run directories found (expected campaign-* or growth_sim-* directories)")
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
    
    # Check structure of first scenario directory (exclude _config; it has its own
    # layout, and exclude the job-level artifact tree _jobs/).
    scenario_dirs = [
        d for d in config_dirs
        if d.name not in ('_config', '_transient', '_execution', '_jobs')
    ]
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

                # Every run links to its job's artifact directory via a `job`
                # symlink pointing into the campaign-level _jobs/ tree.
                job_link = item / 'job'
                if not job_link.is_symlink():
                    print(f"    ✗ 'job' symlink not found in {name} directory")
                    return False
                target = os.readlink(job_link)
                if '_jobs' not in target:
                    print(
                        f"    ✗ 'job' symlink in {name} does not point into _jobs/ "
                        f"(target: {target})"
                    )
                    return False

    print("    ✓ Directory names are valid (numeric run indices)")
    print("    ✓ test.xml files exist in numeric directories")
    print("    ✓ each run has a 'job' symlink into _jobs/")

    # Job-level artifact directories live under <campaign>/_jobs/job-N/.
    if not check_job_directories(first_run):
        return False

    return True


def check_job_directories(campaign_dir):
    """Check the campaign's job-level artifact directories (``_jobs/job-N/``).

    Regardless of ``configs_per_job`` every run is dispatched through a job, so
    the campaign always has a ``_jobs/`` directory with one ``job-N`` subdir per
    job, each holding that job's job-level artifacts (at minimum ``sysinfo.yaml``).
    """
    jobs_dir = campaign_dir / '_jobs'
    if not jobs_dir.is_dir():
        print("  ✗ _jobs directory not found in campaign")
        return False

    job_dirs = [
        d for d in jobs_dir.iterdir()
        if d.is_dir() and d.name.startswith('job-')
    ]
    if not job_dirs:
        print("  ✗ No job-* directories found in _jobs/")
        return False

    print(f"  ✓ Found {len(job_dirs)} job director(y/ies) in _jobs/")

    for job_dir in job_dirs:
        if not (job_dir / 'sysinfo.yaml').exists():
            print(f"    ✗ sysinfo.yaml not found in _jobs/{job_dir.name}/")
            return False

    print("    ✓ sysinfo.yaml exists in each job directory")
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
        if not os.path.exists(os.path.join(test_directory, '.robovast_project')):
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

        # Execute in the test directory where .robovast_project exists
        result = run_command(cmd_postprocess, repo_root, cwd=test_directory)

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


def _find_campaign_dir(results_dir):
    """Return the (single) campaign directory inside a results directory."""
    output_path = Path(results_dir)
    if not output_path.exists():
        return None
    for d in sorted(output_path.iterdir()):
        if d.is_dir() and (
            d.name.startswith('campaign-') or d.name.startswith('growth_sim-')
        ):
            return d
    return None


def _count_job_dirs(campaign_dir):
    """Count ``_jobs/job-N`` directories in a campaign."""
    jobs_dir = campaign_dir / '_jobs'
    if not jobs_dir.is_dir():
        return 0
    return len([
        d for d in jobs_dir.iterdir()
        if d.is_dir() and d.name.startswith('job-')
    ])


def _collect_non_job_files(campaign_dir):
    """Collect campaign-relative file paths, excluding all job-specific artifacts.

    The job *packing* (``configs_per_job``) only changes how runs are grouped
    into jobs; the per-config/per-run scenario output must be identical. This
    returns the set of files that should match regardless of packing by skipping:

    - the ``_jobs/`` artifact tree,
    - the per-run ``job`` symlinks,
    - the ``_transient/`` job bookkeeping (``job_links.yaml``, ``job-N.params.yaml``).
    """
    result = set()
    for root, dirs, files in os.walk(campaign_dir, followlinks=False):
        # Prune the job artifact tree and any symlinked dirs (the `job` links).
        dirs[:] = [
            d for d in dirs
            if d != '_jobs' and not os.path.islink(os.path.join(root, d))
        ]
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), campaign_dir)
            parts = rel.split(os.sep)
            # Skip per-job transient bookkeeping (job_links.yaml, job-N.params.yaml).
            if parts[0] == '_transient' and fn.startswith('job'):
                continue
            result.add(rel)
    return result


def _set_image(text, image):
    """Return *text* with ``execution.image`` replaced by *image*."""
    out = []
    in_execution = False
    replaced = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip('\n')
        if stripped == 'execution:':
            in_execution = True
            out.append(line)
            continue
        if in_execution and stripped.startswith('  image:'):
            out.append(f"  image: {image}\n")
            replaced = True
            continue
        if in_execution and line[:1].strip() and stripped.endswith(':'):
            in_execution = False
        out.append(line)
    if not replaced:
        raise ValueError("Could not find 'image:' in the execution block")
    return ''.join(out)


def _set_configs_per_job(text, value):
    """Return *text* with ``configs_per_job: <value>`` set in the execution block."""
    out = []
    in_execution = False
    inserted = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip('\n')
        if stripped == 'execution:':
            out.append(line)
            out.append(f"  configs_per_job: {value}\n")
            in_execution = True
            inserted = True
            continue
        # Drop any pre-existing configs_per_job entry so ours is authoritative.
        if in_execution and stripped.startswith('  configs_per_job:'):
            continue
        # A new top-level (unindented) key ends the execution block.
        if in_execution and line[:1].strip() and stripped.endswith(':'):
            in_execution = False
        out.append(line)
    if not inserted:
        raise ValueError("Could not find an 'execution:' block in the VAST file")
    return ''.join(out)


def test_configs_per_job_packing(vast_file_path, test_directory, config=None, runs=None):  # pylint: disable=too-many-return-statements
    """Verify configs_per_job>1 packs runs into fewer jobs but keeps output identical.

    Runs the same campaign twice — once with the default packing
    (``configs_per_job=1``, one job per run) and once with
    ``configs_per_job: 10`` temporarily injected into the VAST file — then
    asserts that:

    - the packed run produces fewer jobs (``ceil(N/10)`` instead of ``N``), and
    - every non-job output file is byte-for-byte present in both layouts.
    """
    print("\n" + "=" * 60)
    print("Testing: configs_per_job packing equivalence")
    print("=" * 60)

    repo_root = Path(__file__).parent.parent
    config_path = Path(vast_file_path)
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    if not config_path.exists():
        print(f"✗ Config file not found: {config_path}")
        return False

    # Packing only differs when a job holds more than one work item, so force at
    # least two runs (each run of the selected config is one work item).
    run_count = runs if (runs and runs >= 2) else 2

    base_dir = os.path.join(test_directory, "cpj_base")
    packed_dir = os.path.join(test_directory, "cpj_packed")
    os.makedirs(base_dir, exist_ok=True)
    os.makedirs(packed_dir, exist_ok=True)

    # 1. Baseline: default packing (configs_per_job=1 → one job per run).
    print("\n--- Baseline run (configs_per_job=1) ---")
    if not test_vast_workflow(vast_file_path, base_dir, config, run_count):
        print("✗ Baseline (configs_per_job=1) workflow failed")
        return False

    # 2. Packed: temporarily inject configs_per_job: 10 and re-run.
    print("\n--- Packed run (configs_per_job=10) ---")
    original_text = config_path.read_text(encoding="utf-8")
    try:
        config_path.write_text(
            _set_configs_per_job(original_text, 10), encoding="utf-8"
        )
        if not test_vast_workflow(vast_file_path, packed_dir, config, run_count):
            print("✗ Packed (configs_per_job=10) workflow failed")
            return False
    finally:
        # Always restore the original VAST file, even on failure.
        config_path.write_text(original_text, encoding="utf-8")

    # 3. Compare the two campaign outputs.
    print("\n--- Comparing outputs ---")
    base_campaign = _find_campaign_dir(os.path.join(base_dir, "results"))
    packed_campaign = _find_campaign_dir(os.path.join(packed_dir, "results"))
    if base_campaign is None or packed_campaign is None:
        print("✗ Could not locate campaign directories for comparison")
        return False

    base_jobs = _count_job_dirs(base_campaign)
    packed_jobs = _count_job_dirs(packed_campaign)
    expected_packed = math.ceil(base_jobs / 10) if base_jobs else 0
    print(
        f"  Baseline jobs: {base_jobs}, packed jobs: {packed_jobs} "
        f"(expected {expected_packed})"
    )
    if base_jobs <= packed_jobs:
        print("  ✗ configs_per_job=10 did not reduce the number of jobs")
        return False
    if packed_jobs != expected_packed:
        print(f"  ✗ Unexpected packed job count: {packed_jobs} != {expected_packed}")
        return False
    print("  ✓ Job packing reduced the job count as expected")

    base_files = _collect_non_job_files(base_campaign)
    packed_files = _collect_non_job_files(packed_campaign)
    only_base = base_files - packed_files
    only_packed = packed_files - base_files
    if only_base or only_packed:
        print("  ✗ Non-job output differs between configs_per_job=1 and =10")
        if only_base:
            print(f"    Only in baseline: {sorted(only_base)}")
        if only_packed:
            print(f"    Only in packed:   {sorted(only_packed)}")
        return False

    print(f"  ✓ Non-job output identical ({len(base_files)} files) across packings")
    print("\n✓ configs_per_job packing test succeeded!")
    return True


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
    parser.add_argument(
        '--no-packing-test',
        action='store_true',
        help='Skip the configs_per_job packing-equivalence test (it runs the '
             'campaign twice and is more expensive)'
    )
    parser.add_argument(
        '--image',
        type=str,
        default=None,
        help='Override the container image in the VAST file (e.g. a PR-built image)'
    )

    args = parser.parse_args()

    repo_root = Path(__file__).parent.parent
    config_path = Path(args.vast_file)
    if not config_path.is_absolute():
        config_path = repo_root / config_path

    print("="*60)
    print("VAST Workflow Tests")
    print("="*60)
    print(f"VAST file: {args.vast_file}")
    if args.config:
        print(f"Configuration: {args.config}")
    if args.test_directory:
        print(f"Test directory: {args.test_directory}")
    if args.image:
        print(f"Image override: {args.image}")

    original_text = None
    if args.image:
        original_text = config_path.read_text(encoding='utf-8')
        config_path.write_text(_set_image(original_text, args.image), encoding='utf-8')

    try:
        tests = [
            ("Complete workflow: init -> execution -> postprocess", test_vast_workflow, args.vast_file, args.test_directory, args.config, args.runs),
        ]
        if not args.no_packing_test:
            tests.append(
                ("configs_per_job packing equivalence", test_configs_per_job_packing,
                 args.vast_file, args.test_directory, args.config, args.runs)
            )

        results = []
        for name, test_func, *test_args in tests:
            try:
                result = test_func(*test_args)
                results.append((name, result))
            except Exception as e:
                print(f"✗ Test '{name}' raised exception: {e}")
                traceback.print_exc()
                results.append((name, False))

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
    finally:
        if original_text is not None:
            config_path.write_text(original_text, encoding='utf-8')


if __name__ == '__main__':
    sys.exit(main())
