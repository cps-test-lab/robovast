#!/usr/bin/env python3
"""
Local Execution Widget - A widget for displaying and managing local execution information
"""

import sys
from pathlib import Path

from .common import clean_test_name
from .terminal_output_widget import TerminalOutputWidget

try:
    from PySide2.QtCore import QProcess, QThread, Signal
    from PySide2.QtGui import QFont
    from PySide2.QtWidgets import (QApplication, QHBoxLayout, QLabel,
                                   QPushButton, QVBoxLayout, QWidget)
    QT_SUPPORT = True
except ImportError:
    print("Error: PySide2 is required. Install it with:")
    print("  pip install PySide2")
    sys.exit(1)


class LocalExecutionWorker(QThread):
    """Worker thread for running local execution and capturing output"""
    output_received = Signal(str)
    execution_finished = Signal(int)
    stop_requested = Signal()

    def __init__(self, command, working_dir=None):
        super().__init__()
        self.command = command
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

            # Create a script that sets up a new process group and runs the command
            # This ensures all child processes can be killed together
            script = f"""#!/bin/bash
set -e
# Create new process group
setsid bash -c '{self.command}' &
PID=$!
# Wait for the process and forward signals
trap 'kill -TERM -$PID 2>/dev/null || true; wait $PID' TERM
wait $PID
"""

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
            # Strip trailing newlines to avoid double newlines
            stdout = stdout.rstrip('\n\r')
            if stdout:  # Only emit non-empty output
                self.output_received.emit(stdout)

    def handle_stderr(self):
        """Handle standard error"""
        if self.process:
            data = self.process.readAllStandardError()
            stderr = bytes(data).decode("utf8")
            # Strip trailing newlines to avoid double newlines
            stderr = stderr.rstrip('\n\r')
            if stderr:  # Only emit non-empty output
                self.output_received.emit(stderr)

    def handle_finished(self, exit_code):
        """Handle process completion"""
        self.execution_finished.emit(exit_code)
        # Don't quit here - let the run() method handle cleanup


class LocalExecutionWidget(QWidget):
    """Widget for managing local execution"""

    def __init__(self):
        super().__init__()
        self.current_test_dir = None
        self.is_running = False
        self.execution_worker = None
        self.setup_ui()

    def setup_ui(self):
        """Setup the local execution UI"""
        layout = QVBoxLayout(self)

        # Command label
        self.command_label = QLabel("No test selected")
        self.command_label.setWordWrap(True)
        self.command_label.setFont(QFont("Courier", 10))
        layout.addWidget(self.command_label)

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

        # Copy button
        self.copy_btn = QPushButton("📋 Copy Command")
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self.copy_command)
        self.copy_btn.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 6px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #5a6268;
            }
            QPushButton:disabled {
                background-color: #6c757d;
                color: #adb5bd;
            }
        """)
        button_layout.addWidget(self.copy_btn)

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

        # Terminal output
        terminal_label = QLabel("Terminal Output:")
        terminal_label.setStyleSheet("font-weight: bold; margin-top: 10px;")
        layout.addWidget(terminal_label)

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
            self.command_label.setText("No test selected")
            self.execute_btn.setEnabled(False)
            self.copy_btn.setEnabled(False)
            return

        # Check if this is a valid test directory
        scenario_file = self.current_test_dir / "scenario.osc"
        if not scenario_file.exists():
            self.command_label.setText("Invalid test directory - no scenario.osc file found")
            self.execute_btn.setEnabled(False)
            self.copy_btn.setEnabled(False)
            return

        # Build command in the specified format
        # Use relative path from workspace root
        relative_scenario_path = f"scenarios/Dataset/{scenario_file.name}"

        # Remove the last -<number> pattern from test_name for run_local.sh
        test_name_cleaned = clean_test_name(self.current_test_dir)

        command = f"kubernetes/run_local.sh {relative_scenario_path} {test_name_cleaned}"

        # Note: variant file is handled by the script, not as a separate parameter
        self.command_label.setText(command)
        self.execute_btn.setEnabled(not self.is_running)
        self.copy_btn.setEnabled(True)

    def execute_command(self):
        """Execute the current command"""
        if not self.current_test_dir or self.is_running:
            return

        command = self.command_label.text().strip()
        if not command or command == "No test selected":
            return

        self.is_running = True
        self.execute_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        # Clear previous output
        self.terminal_output.clear()
        self.append_output(f"🚀 Starting local execution: {self.current_test_dir.name}")
        self.append_output(f"💻 Command: {command}")
        self.append_output("=" * 60)

        try:
            # Get the base directory by going up from the test directory
            base_dir = self.current_test_dir.parent.parent.parent.parent  # Assuming test is in scenarios/Dataset/

            # Start execution in worker thread
            self.execution_worker = LocalExecutionWorker(command, str(base_dir))
            self.execution_worker.output_received.connect(self.append_output)
            self.execution_worker.execution_finished.connect(self.on_execution_finished)
            self.execution_worker.start()

        except Exception as e:
            self.append_output(f"Error executing command: {str(e)}")
            self.on_execution_finished(-1)

    def copy_command(self):
        """Copy command to clipboard"""
        command = self.command_label.text().strip()
        if command and command != "No test selected":
            clipboard = QApplication.clipboard()
            clipboard.setText(command)
            self.append_output("📋 Command copied to clipboard")

    def append_output(self, text):
        """Append text to terminal output with syntax highlighting"""
        self.terminal_output.append_output(text)

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

    def reset_button(self):
        """Reset the execute button state"""
        self.is_running = False
        self.execute_btn.setEnabled(True)

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


def test_widget():
    """Test the local execution widget"""
    import sys

    from PySide2.QtWidgets import (QApplication, QMainWindow, QVBoxLayout,
                                   QWidget)

    app = QApplication(sys.argv)

    # Create main window
    window = QMainWindow()
    window.setWindowTitle("Local Execution Widget Test")
    window.setGeometry(100, 100, 600, 200)

    # Create central widget
    central_widget = QWidget()
    window.setCentralWidget(central_widget)
    layout = QVBoxLayout(central_widget)

    # Add local execution widget
    local_execution = LocalExecutionWidget()
    layout.addWidget(local_execution)

    window.show()

    print("Local execution widget test window created.")

    sys.exit(app.exec_())


if __name__ == "__main__":
    test_widget()
