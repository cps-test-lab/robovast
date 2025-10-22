#!/usr/bin/env python3
"""
Command Line Execution Worker - Executes external commands with test directory as parameter
"""

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from robovast_common import FileCache

from .common import RUN_TYPE
from .settings_dialog import SettingsDialog
from .worker_thread import CancellableWorkload


class CommandLineExecutionWorker(CancellableWorkload):
    """Worker for executing external command-line processes with test directory path"""

    def __init__(self, settings_key):
        super().__init__("RosbagConversion")
        self.process = None
        self.settings_key = settings_key

    def cancel(self):
        """Cancel the execution"""
        if self.process:
            try:
                self.process.terminate()
                # Give it a moment to terminate gracefully
                time.sleep(0.5)
                if self.process.poll() is None:
                    self.process.kill()
            except:
                pass
        super().cancel()

    @staticmethod
    def get_rosbag_paths(data_path, run_type):
        """Determine CSV files and analysis type based on directory structure"""
        try:
            if run_type == RUN_TYPE.SINGLE_TEST:
                # 1. Check if CSV file exists directly in the path (single run)
                if os.path.exists(os.path.join(data_path, "rosbag2")):
                    return [Path(data_path) / "rosbag2"]
                else:
                    return None
            elif run_type == RUN_TYPE.SINGLE_VARIANT:
                # 2. Check if CSV files exist in subfolders (folder run)
                dirs = list(data_path.glob("*/rosbag2"))
                if dirs:
                    return dirs
                else:
                    return None
            elif run_type == RUN_TYPE.RUN:
                # 3. Check if CSV files exist in subfolders of subfolders (whole run)
                dirs = list(data_path.glob("*/*/rosbag2"))
                if dirs:
                    return dirs
                else:
                    return None

        except Exception as e:
            return None

        return None

    def run(self, test_directory_path, run_type):
        """Execute the external command with test directory as last parameter"""

        # Record start time for execution measurement
        start_time = time.time()

        # Get command from settings
        command_line = SettingsDialog.get_setting(self.settings_key, str)

        if not command_line.strip():
            print("No external command configured - skipping command execution")
            return False, "No command configured"

        # Extract the executable from the command line
        executable = shlex.split(command_line)[0]

        # Check if the executable exists in the system PATH
        if not shutil.which(executable):
            print(f"Executable not found: {executable}")
            return False, f"Executable not found: {executable}"

        if not command_line.strip():
            print("No external command configured - skipping command execution")
            return False, "No command configured"
        file_cache = FileCache()
        file_cache.set_current_data_directory(test_directory_path)

        rosbag_paths = CommandLineExecutionWorker.get_rosbag_paths(test_directory_path, run_type)
        if not rosbag_paths:
            print(f"No rosbag2 folders found in {test_directory_path} for run type {run_type.name}")
            return False, f"No rosbag2 folders found for run type {run_type.name}"

        print(f"CommandLineExecutionWorker started for: {test_directory_path} run_type: {run_type}")
        count = 0
        max_count = len(rosbag_paths)
        overall_result = True
        for rosbag_path in rosbag_paths:
            if self.is_cancelled():
                return False, None

            relative_path = str(rosbag_path.relative_to(test_directory_path).parent).replace(os.sep, "_")

            self.progress_callback(count/max_count*100, f"Converting rosbag {rosbag_path}..")
            count += 1

            if not os.path.exists(rosbag_path):
                print(f"rosbag2 {rosbag_path} does not exist. Skipping...")
                overall_result = False
                continue

            if os.path.isdir(rosbag_path) and os.path.exists(rosbag_path / "rosbag2_0.mcap") and not os.path.exists(rosbag_path / "metadata.yaml"):
                print(f"WARNING: Rosbag2 {rosbag_path} is missing metadata.yaml. Reindexing...")
                try:
                    command_parts = ["ros2", "bag", "reindex"]
                    command_parts.append(str(rosbag_path))

                    print(f"Executing command: {' '.join(command_parts)}")

                    # Start the process
                    self.process = subprocess.Popen(
                        command_parts,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        cwd=os.getcwd(),  # Use current working directory
                        env=os.environ.copy()  # Use current environment
                    )

                except Exception as e:
                    error_msg = f"Error reindexing with external command: {str(e)}"
                    print(error_msg)
                finally:
                    self.process = None

            files_for_hash = [str(file_name.relative_to(rosbag_path)) for file_name in Path(rosbag_path).glob("*") if file_name.is_file()]
            # Add the executable to the files_for_hash
            files_for_hash.append(executable)

            if not relative_path or relative_path == ".":
                hash_file_name = "rosbag2.csv"
            else:
                hash_file_name = f"rosbag2_{relative_path}.csv"
            cached_file = file_cache.get_cached_file(files_for_hash, hash_file_name, binary=False, content=False, strings_for_hash=[
                                                     SettingsDialog.get_setting(self.settings_key, str)])

            if cached_file is None:
                print("No cached file found, executing command")
                cached_file = file_cache.get_cache_filename(hash_file_name)
            else:
                print(f"Use cached file {cached_file}, skipping execution")
                continue

            try:
                # Parse the command line and append the test directory path
                command_parts = shlex.split(command_line)
                command_parts.append("--input")
                command_parts.append(str(rosbag_path))
                command_parts.append("--output")
                command_parts.append(cached_file)

                print(f"Executing command: {' '.join(command_parts)}")

                # Start the process
                self.process = subprocess.Popen(
                    command_parts,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=os.getcwd(),  # Use current working directory
                    env=os.environ.copy()  # Use current environment
                )

                # Monitor the process with periodic cancellation checks
                stdout_lines = []
                stderr_lines = []

                while self.process.poll() is None:
                    if self.is_cancelled():
                        self.cancel()
                        return False, None

                    time.sleep(0.1)  # Small delay to avoid busy waiting

                # Process finished, collect output
                stdout, stderr = self.process.communicate()
                return_code = self.process.returncode

                if self.is_cancelled():
                    return False, None

                # Calculate execution time
                end_time = time.time()
                execution_time = end_time - start_time

                # Prepare result
                result = {
                    'command': ' '.join(command_parts),
                    'return_code': return_code,
                    'stdout': stdout.strip() if stdout else '',
                    'stderr': stderr.strip() if stderr else '',
                    'success': return_code == 0,
                    'test_directory': str(test_directory_path)
                }

                if return_code == 0:
                    print(f"External command completed successfully in {execution_time:.2f} seconds")
                    file_cache.save_file_to_cache(files_for_hash, hash_file_name, None, binary=False, content=False,
                                                  strings_for_hash=[SettingsDialog.get_setting(self.settings_key, str)])

                else:
                    print(f"External command failed with return code {return_code} after {execution_time:.2f} seconds")
                    print(f"STDERR: {stderr}")

                if return_code != 0:
                    overall_result = False

            except FileNotFoundError as e:
                end_time = time.time()
                execution_time = end_time - start_time
                error_msg = f"Command not found: {e}"
                print(f"Error executing external command: {error_msg}")
                return False, {
                    'command': command_line,
                    'return_code': -1,
                    'stdout': '',
                    'stderr': error_msg,
                    'success': False,
                    'test_directory': str(test_directory_path),
                    'error': error_msg
                }

            except Exception as e:
                end_time = time.time()
                execution_time = end_time - start_time
                error_msg = f"Error executing external command: {str(e)}"
                print(error_msg)
                return False, {
                    'command': command_line,
                    'return_code': -1,
                    'stdout': '',
                    'stderr': error_msg,
                    'success': False,
                    'test_directory': str(test_directory_path),
                    'error': error_msg,
                    'execution_time': execution_time
                }
            finally:
                self.process = None
        return overall_result, None
