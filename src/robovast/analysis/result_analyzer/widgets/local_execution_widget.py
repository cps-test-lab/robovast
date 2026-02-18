#!/usr/bin/env python3
# Copyright (C) 2025 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from PySide6.QtCore import QProcess, QThread, Signal
from PySide6.QtWidgets import (QFormLayout, QHBoxLayout, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

from .terminal_output_widget import TerminalOutputWidget


class LocalExecutionWorker(QThread):
    """Worker thread for running local execution and capturing output"""
    output_received = Signal(str)
    execution_finished = Signal(int)
    stop_requested = Signal()

    def __init__(self, config_file, config_name, working_dir=None):
        super().__init__()
        self.config_file = config_file
        self.config_name = config_name
        self.working_dir = working_dir
        self.process = None
        self._should_stop = False
        # Connect the stop signal to our internal slot
        self.stop_requested.connect(self._handle_stop_request)

    def run(self):
        """Run the local execution command"""
        try:
            self.process = QProcess()
            self.process.setWorkingDirectory(self.working_dir)
            self.process.readyReadStandardOutput.connect(self.handle_stdout)
            self.process.readyReadStandardError.connect(self.handle_stderr)
            self.process.finished.connect(self.handle_finished)

            # Output will automatically go to project results_dir
            command = f"vast execution local run -r 1 -c {self.config_name}"

            # Create a script that sets up a new process group and runs the command
            # This ensures all child processes can be killed together
            script = f"""#!/bin/bash
set -e
# Create new process group
setsid bash -c '{command}' &
PID=$!
# Wait for the process and forward signals
trap 'kill -TERM -$PID 2>/dev/null || true; wait $PID' TERM
wait $PID
"""

            self.output_received.emit(f"🚀 Starting local execution...")

            self.output_received.emit(f"💻 Command: {command}")
            self.output_received.emit("=" * 60)

            # Start the process with the script
            self.process.start("bash", ["-c", script])

            # Wait for the process to finish or stop signal
            self.exec_()  # Use event loop instead of waitForFinished

        except Exception as e:
            self.output_received.emit(f"Error executing command: {str(e)}")
            self.execution_finished.emit(-1)
        finally:
            # Ensure process is properly terminated before exit
            self._cleanup_process()
            # Make sure we exit the event loop
            self.quit()

    def _cleanup_process(self):
        """Ensure the process is properly terminated while capturing remaining output"""
        if self.process:
            if self.process.state() == QProcess.Running:
                # Send SIGTERM to the process which will propagate to the process group
                self.process.terminate()

                # Wait for termination while continuing to read output
                if not self.process.waitForFinished(3000):  # Wait 3 seconds
                    # If still running, force kill
                    self.process.kill()
                    self.process.waitForFinished(1000)

                # After process finishes, read any remaining output
                self.handle_stdout()
                self.handle_stderr()

            # Disconnect all signals to prevent further callbacks
            self.process.readyReadStandardOutput.disconnect()
            self.process.readyReadStandardError.disconnect()
            self.process.finished.disconnect()

            # Don't use deleteLater() here as we're in the same thread
            self.process = None

    def _handle_stop_request(self):
        """Handle stop request from main thread - runs in worker thread"""
        # Don't call cleanup here, just quit the event loop
        # Cleanup will happen in the finally block of run()
        self.quit()  # Exit the event loop

    def request_stop(self):
        """Request stop from main thread - thread-safe"""
        self.stop_requested.emit()

    def handle_stdout(self):
        """Handle standard output"""
        if self.process:
            data = self.process.readAllStandardOutput()
            stdout = bytes(data).decode("utf8")
            if stdout:  # Only emit non-empty output
                # Process line by line to preserve structure
                lines = stdout.splitlines()
                for line in lines:
                    self.output_received.emit(line)

    def handle_stderr(self):
        """Handle standard error"""
        if self.process:
            data = self.process.readAllStandardError()
            stderr = bytes(data).decode("utf8")
            if stderr:  # Only emit non-empty output
                # Process line by line to preserve structure
                lines = stderr.splitlines()
                for line in lines:
                    self.output_received.emit(line)

    def handle_finished(self, exit_code):
        """Handle process completion"""
        self.execution_finished.emit(exit_code)
        # Don't quit here - let the run() method handle cleanup


class LocalExecutionWidget(QWidget):
    """Widget for managing local execution"""

    def __init__(self, config_file):
        super().__init__()
        self.config_file = config_file
        self.current_test_dir = None
        self.is_running = False
        self.execution_worker = None
        self.setup_ui()

    def setup_ui(self):
        """Setup the local execution UI"""
        layout = QVBoxLayout(self)

        # Button layout
        button_layout = QHBoxLayout()

        # Execute button
        self.execute_btn = QPushButton("Execute")
        self.execute_btn.setEnabled(False)
        self.execute_btn.clicked.connect(self.execute_command)
        self.execute_btn.setStyleSheet("""
            QPushButton {
                background-color: #007bff;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 6px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #0056b3;
            }
            QPushButton:disabled {
                background-color: #6c757d;
                color: #adb5bd;
            }
        """)
        button_layout.addWidget(self.execute_btn)

        # Stop button
        self.stop_btn = QPushButton("⏹️ Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_execution)
        self.stop_btn.setStyleSheet("""
            QPushButton {
                background-color: #dc3545;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 6px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #c82333;
            }
            QPushButton:disabled {
                background-color: #6c757d;
                color: #adb5bd;
            }
        """)
        button_layout.addWidget(self.stop_btn)

        button_layout.addStretch()
        layout.addLayout(button_layout)

        # Search form layout
        search_layout = QFormLayout()

        # Search input
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search in terminal output...")
        self.search_input.textChanged.connect(self.on_search_changed)
        search_layout.addRow("Search:", self.search_input)

        layout.addLayout(search_layout)

        self.terminal_output = TerminalOutputWidget()
        layout.addWidget(self.terminal_output)

        # Set minimum height for the widget
        self.setMinimumHeight(400)

    def set_test_directory(self, directory_path):
        """Set the current test directory and update UI"""
        self.current_test_dir = Path(directory_path) if directory_path else None
        self.update_test_info()

    def update_test_info(self):
        """Update test information display"""
        if not self.current_test_dir or not self.current_test_dir.exists():
            self.execute_btn.setEnabled(False)
            return

        # Check if this is a valid test directory
        scenario_file = self.current_test_dir / "test.xml"
        if not scenario_file.exists():
            self.execute_btn.setEnabled(False)
            return

        # Note: config file is handled by the script, not as a separate parameter
        self.execute_btn.setEnabled(not self.is_running)

    def execute_command(self):
        """Execute the current command"""
        if not self.current_test_dir or self.is_running:
            return

        self.is_running = True
        self.execute_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        # Clear previous output
        self.terminal_output.clear()

        try:
            # Get the base directory by going up from the test directory
            base_dir = self.current_test_dir.parent.parent.parent.parent  # Assuming test is in scenarios/Dataset/

            # Remove the last -<number> pattern from test_name for run_local.sh
            test_name_cleaned = self.current_test_dir.parent.name

            # Start execution in worker thread
            self.execution_worker = LocalExecutionWorker(self.config_file, test_name_cleaned, str(base_dir))
            self.execution_worker.output_received.connect(self.append_output)
            self.execution_worker.execution_finished.connect(self.on_execution_finished)
            self.execution_worker.start()

        except Exception as e:
            self.append_output(f"Error executing command: {str(e)}")
            self.on_execution_finished(-1)

    def append_output(self, text):
        """Append text to terminal output with syntax highlighting"""
        self.terminal_output.append_output(text)

    def on_search_changed(self, search_text):
        """Handle search text changes"""
        # Clear existing highlighting
        self.terminal_output.clear_highlighting()

        # Apply search highlighting if there's search text
        if search_text.strip():
            self.terminal_output.highlight_search_term(search_text.strip())

    def on_execution_finished(self, return_code):
        """Handle local execution completion"""
        # Only handle if we haven't already stopped manually
        if not self.execution_worker:
            return

        self.is_running = False
        self.execute_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

        if return_code == 0:
            self.append_output("=" * 60)
            self.append_output("✅ Local execution completed successfully!")
        elif return_code == -1:
            self.append_output("=" * 60)
            self.append_output("⏹️ Local execution stopped")
        else:
            self.append_output("=" * 60)
            self.append_output(f"❌ Local execution failed with return code: {return_code}")

        # Clean up worker - wait for thread to finish properly
        if self.execution_worker:
            # Signal the worker to quit its event loop
            self.execution_worker.quit()
            # Wait for the thread to finish
            if self.execution_worker.wait(3000):  # Wait up to 3 seconds
                self.execution_worker.deleteLater()
            else:
                # Force terminate if it doesn't finish cleanly
                self.execution_worker.terminate()
                self.execution_worker.wait(1000)
                self.execution_worker.deleteLater()
            self.execution_worker = None

    def stop_execution(self):
        """Stop any running execution"""
        if self.execution_worker and self.execution_worker.isRunning():
            # Request stop using signal - this is thread-safe
            self.execution_worker.request_stop()

            # Wait for the thread to finish
            if not self.execution_worker.wait(5000):  # Wait up to 5 seconds
                # If it still doesn't finish, force terminate as last resort
                self.execution_worker.terminate()
                self.execution_worker.wait(1000)

            # Clean up
            if self.execution_worker:
                self.execution_worker.deleteLater()
                self.execution_worker = None

        # Update UI state
        self.is_running = False
        self.execute_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.append_output("🛑 Local execution stopped by user")
