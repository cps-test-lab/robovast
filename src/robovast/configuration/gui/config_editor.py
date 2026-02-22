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

"""PySide6 GUI for editing and validating YAML configuration files."""

import os
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QIcon, QKeyEvent
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QPushButton,
                               QSplitter, QTextEdit, QVBoxLayout, QWidget)
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from robovast.common import generate_scenario_variations
from robovast.common.config import validate_config
from robovast.configuration.gui.config_list import ConfigList
from robovast.configuration.gui.config_view import ConfigView
from robovast.configuration.gui.yaml_editor import YamlEditor


class GenerationWorker(QObject):
    """Worker class for generating scenario variations in a separate thread."""

    # Signals
    progress = Signal(str)  # Progress message
    finished = Signal()  # Configs list, GUI classes dict
    error = Signal(str)  # Error message
    cancelled = Signal()  # Cancellation signal

    def __init__(self, yaml_path, output_dir):
        super().__init__()
        self.yaml_path = yaml_path
        self.output_dir = output_dir
        self.configs = []
        self.variation_gui_classes = {}
        self._cancelled = False
        self._cancel_message_shown = False

    def cancel(self):
        """Request cancellation of the generation process."""
        self._cancelled = True

    def _check_interruption(self, msg):
        """Check for interruption request and emit progress.

        This method must never raise exceptions to avoid Qt event handler errors.
        """
        try:
            # Check if cancellation was requested
            if self._cancelled or QThread.currentThread().isInterruptionRequested():
                self._cancelled = True
                # Emit progress message about cancellation only once
                if not self._cancel_message_shown:
                    self.progress.emit("Generation cancelled by user")
                    self._cancel_message_shown = True
                return
            # Emit normal progress message
            self.progress.emit(msg)
        except Exception:
            # Catch any exception to prevent it from propagating through Qt
            pass

    def run(self):
        """Run the generation process."""
        try:
            run_data, variation_gui_classes = generate_scenario_variations(
                variation_file=self.yaml_path,
                progress_update_callback=self._check_interruption,
                output_dir=self.output_dir
            )

            # Check if we were cancelled during generation
            if self._cancelled:
                self.cancelled.emit()
                return

            self.configs = run_data["configs"]
            self.variation_gui_classes = variation_gui_classes
            self.finished.emit()
        except Exception as e:
            if not self._cancelled:
                self.error.emit(str(e))


class ConfigEditor(QMainWindow):
    """Main window for the configuration editor."""

    def __init__(self, project_config=None, debug=False):
        super().__init__()
        self.current_file = None
        self.temp_yaml_path = None
        self.temp_config = None
        self.temp_dir = None
        self.debug = debug
        self.yaml_parser = YAML()
        self.yaml_parser.preserve_quotes = True
        self.yaml_parser.default_flow_style = False

        # Initialize QSettings for storing window geometry
        self.settings = QSettings("RoboVAST", "ConfigEditor")

        # Thread and worker for generation
        self.generation_thread = None
        self.generation_worker = None

        self.init_ui()

        # Restore window geometry
        self.restore_geometry()

        # Set up validation timer (debounce validation)
        self.validation_timer = QTimer()
        self.validation_timer.setSingleShot(True)
        self.validation_timer.timeout.connect(self.validate_config)

        # Connect text changed signal
        self.editor.textChanged.connect(self.on_text_changed)

        if not project_config:
            raise ValueError("Project configuration must be provided")
        if project_config.config_path:
            try:
                with open(project_config.config_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                self.editor.setPlainText(content)
                self.current_file = project_config.config_path
                self.setWindowTitle(f"Configuration Editor - {Path(project_config.config_path).name}")
                self.validate_config()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to open file:\n{str(e)}")

    def init_ui(self):
        """Initialize the user interface."""
        self.setWindowTitle("Configuration Editor")
        self.setGeometry(100, 100, 1600, 800)

        # Set window icon
        icon_path = Path(__file__).parent.parent.parent.parent.parent / "docs" / "images" / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        # Create central widget and layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Create horizontal splitter for 3 columns: editor+error, config list, config view
        self.main_splitter = QSplitter(Qt.Horizontal)

        # Column 1: Create vertical splitter for editor and error display
        self.left_splitter = QSplitter(Qt.Vertical)

        # Create a widget to hold the editor and buttons
        editor_container = QWidget()
        editor_container_layout = QVBoxLayout(editor_container)
        editor_container_layout.setContentsMargins(0, 0, 0, 0)

        # Add label for Configuration
        label = QLabel("Configuration")
        editor_container_layout.addWidget(label)

        # Create YAML editor
        self.editor = YamlEditor()
        self.editor.setPlaceholderText("Enter or load vast configuration here...")
        editor_container_layout.addWidget(self.editor)

        # Create button container with horizontal layout
        button_container = QWidget()
        button_layout = QHBoxLayout(button_container)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(5)

        # Add Generate button
        self.generate_button = QPushButton("Generate")
        self.generate_button.setMaximumWidth(150)
        self.generate_button.clicked.connect(self.on_generate_clicked)
        self.generate_button.setStyleSheet("""
            QPushButton {
                background-color: #0e639c;
                color: white;
                border: 1px solid #1177bb;
                padding: 5px 15px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #1177bb;
            }
            QPushButton:pressed {
                background-color: #0d5689;
            }
            QPushButton:disabled {
                background-color: #555555;
                color: #888888;
                border: 1px solid #444444;
            }
        """)
        button_layout.addWidget(self.generate_button)

        # Add Cancel button (initially hidden)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setMaximumWidth(80)
        self.cancel_button.clicked.connect(self.on_cancel_clicked)
        self.cancel_button.setVisible(False)
        self.cancel_button.setStyleSheet("""
            QPushButton {
                background-color: #a1260d;
                color: white;
                border: 1px solid #c9302c;
                padding: 5px 10px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #c9302c;
            }
            QPushButton:pressed {
                background-color: #8a1f0a;
            }
        """)
        button_layout.addWidget(self.cancel_button)

        # Add Save button
        self.save_button = QPushButton("Save")
        self.save_button.setMaximumWidth(150)
        self.save_button.clicked.connect(self.on_save_clicked)
        self.save_button.setStyleSheet("""
            QPushButton {
                background-color: #2d7d2d;
                color: white;
                border: 1px solid #3a9a3a;
                padding: 5px 15px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #3a9a3a;
            }
            QPushButton:pressed {
                background-color: #246624;
            }
            QPushButton:disabled {
                background-color: #555555;
                color: #888888;
                border: 1px solid #444444;
            }
        """)
        button_layout.addWidget(self.save_button)

        # Add Save As button
        self.save_as_button = QPushButton("Save As")
        self.save_as_button.setMaximumWidth(150)
        self.save_as_button.clicked.connect(self.on_save_as_clicked)
        self.save_as_button.setStyleSheet("""
            QPushButton {
                background-color: #2d7d2d;
                color: white;
                border: 1px solid #3a9a3a;
                padding: 5px 15px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #3a9a3a;
            }
            QPushButton:pressed {
                background-color: #246624;
            }
            QPushButton:disabled {
                background-color: #555555;
                color: #888888;
                border: 1px solid #444444;
            }
        """)
        button_layout.addWidget(self.save_as_button)

        editor_container_layout.addWidget(button_container)

        self.left_splitter.addWidget(editor_container)

        # Create error display area
        error_widget = QWidget()
        error_layout = QVBoxLayout(error_widget)
        error_layout.setContentsMargins(0, 0, 0, 0)

        self.error_display = QTextEdit()
        self.error_display.setReadOnly(True)
        self.error_display.setMaximumHeight(200)
        self.error_display.setStyleSheet("""
            QTextEdit {
                background-color: #2b2b2b;
                color: #cccccc;
                border: 1px solid #555;
                padding: 5px;
            }
        """)
        error_layout.addWidget(self.error_display)

        self.left_splitter.addWidget(error_widget)

        # Set left splitter sizes (editor gets more space)
        self.left_splitter.setSizes([600, 200])

        # Add column 1 to main splitter
        self.main_splitter.addWidget(self.left_splitter)

        # Column 2: Create config list (full height)
        self.config_list = ConfigList()
        self.main_splitter.addWidget(self.config_list)

        # Column 3: Create config view (full height)
        self.config_view = ConfigView(debug=self.debug)
        self.main_splitter.addWidget(self.config_view)

        # Set main splitter sizes for 3 columns (editor+error, config list, config view)
        self.main_splitter.setSizes([700, 200, 500])

        # Connect config list selection to config view
        self.config_list.config_selected.connect(self.config_view.update_config_info)

        layout.addWidget(self.main_splitter)

        # Apply dark theme
        self.apply_dark_theme()

    def restore_geometry(self):
        """Restore window size and position from settings."""
        # Restore window geometry (size and position)
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            # Use default size if no saved geometry
            self.setGeometry(100, 100, 1600, 800)

        # Restore splitter states
        main_splitter_state = self.settings.value("main_splitter")
        if main_splitter_state:
            self.main_splitter.restoreState(main_splitter_state)

        left_splitter_state = self.settings.value("left_splitter")
        if left_splitter_state:
            self.left_splitter.restoreState(left_splitter_state)

    def save_geometry(self):
        """Save window size and position to settings."""
        self.settings.setValue("geometry", self.saveGeometry())

        # Save splitter states
        self.settings.setValue("main_splitter", self.main_splitter.saveState())
        self.settings.setValue("left_splitter", self.left_splitter.saveState())

    def closeEvent(self, event):
        """Handle window close event to save geometry."""
        self.cleanup_generation()
        self.save_geometry()
        event.accept()

    def apply_dark_theme(self):
        """Apply a dark color scheme to the application."""
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1e1e1e;
            }
            QMenuBar {
                background-color: #2d2d30;
                color: #cccccc;
            }
            QMenuBar::item:selected {
                background-color: #3e3e42;
            }
            QMenu {
                background-color: #2d2d30;
                color: #cccccc;
                border: 1px solid #555;
            }
            QMenu::item:selected {
                background-color: #3e3e42;
            }
            QPlainTextEdit {
                background-color: #1e1e1e;
                color: #d4d4d4;
                border: 1px solid #555;
                selection-background-color: #264f78;
            }
            QLabel {
                color: #cccccc;
            }
            QTabWidget::pane {
                border: 1px solid #555;
                background-color: #1e1e1e;
            }
            QTabBar::tab {
                background-color: #2d2d30;
                color: #cccccc;
                border: 1px solid #555;
                padding: 8px 20px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background-color: #1e1e1e;
                border-bottom-color: #1e1e1e;
            }
            QTabBar::tab:hover {
                background-color: #3e3e42;
            }
        """)

    def save_to_file(self, file_name):
        """Save content to the specified file."""
        try:
            with open(file_name, 'w', encoding='utf-8') as f:
                f.write(self.editor.toPlainText())
            return True
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save file:\n{str(e)}")
            return False

    def confirm_discard_changes(self):
        """Ask user to confirm discarding changes."""
        reply = QMessageBox.question(
            self,
            "Discard Changes",
            "Current content will be lost. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        return reply == QMessageBox.Yes

    def on_text_changed(self):
        """Handle text changes in the editor."""
        # Restart the validation timer (debounce)
        self.validation_timer.stop()
        self.validation_timer.start(500)  # Wait 500ms after user stops typing

    def on_generate_clicked(self):
        """Handle Generate button click."""

        # Prevent multiple simultaneous generations
        if self.generation_thread and self.generation_thread.isRunning():
            self.error_display.append("<span style='color: #f48771;'>Generation already in progress...</span>")
            return

        # Clear error display and show progress
        self.error_display.clear()
        self.error_display.append("<span style='color: #4ec9b0;'>Starting generation...</span>")

        # Clear config list and config view
        self.config_list.clear()
        self.config_view.clear()

        # Update button states for generation
        self.generate_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.generate_button.setMaximumWidth(100)  # Reduce size
        self.cancel_button.setVisible(True)  # Show cancel button

        # Get YAML content from editor
        yaml_text = self.editor.toPlainText().strip()

        if not yaml_text:
            self.error_display.setHtml("<span style='color: #f48771;'><b>Error:</b> No YAML configuration to generate from.</span>")
            self.reset_buttons_to_idle()
            return

        # Save YAML to a temporary file
        try:
            self.temp_config = tempfile.NamedTemporaryFile(mode='w', suffix='.vast',
                                                           prefix='.robovast_temp_variation_config_',
                                                           dir=os.path.abspath(os.path.dirname(self.current_file)))
            self.temp_config.write(yaml_text)
            self.temp_config.flush()

            # Create temporary output directory
            self.temp_dir = tempfile.TemporaryDirectory(prefix='.robovast_temp_variation_output_')

            # Create worker and thread
            self.generation_thread = QThread()
            self.generation_worker = GenerationWorker(self.temp_config.name, self.temp_dir.name)

            # Move worker to thread
            self.generation_worker.moveToThread(self.generation_thread)

            # Connect signals
            self.generation_thread.started.connect(self.generation_worker.run)
            self.generation_worker.progress.connect(self.on_generation_progress)
            self.generation_worker.finished.connect(self.on_generation_finished)
            self.generation_worker.error.connect(self.on_generation_error)
            self.generation_worker.cancelled.connect(self.on_generation_cancelled)

            # Clean up thread when finished
            self.generation_worker.finished.connect(self.generation_thread.quit)
            self.generation_worker.error.connect(self.generation_thread.quit)
            self.generation_worker.cancelled.connect(self.generation_thread.quit)
            self.generation_thread.finished.connect(self.reset_buttons_to_idle)

            # Start the thread
            self.generation_thread.start()

        except Exception as e:
            error_msg = f"<span style='color: #f48771;'><b>Setup Error:</b></span><br>"
            error_msg += f"<span style='color: #dcdcaa;'>{str(e)}</span>"
            self.error_display.setHtml(error_msg)
            self.reset_buttons_to_idle()

    def reset_buttons_to_idle(self):
        """Reset button states to idle after generation completes or is cancelled."""
        self.generate_button.setEnabled(True)
        self.save_button.setEnabled(True)
        self.generate_button.setMaximumWidth(150)  # Restore original size
        self.cancel_button.setVisible(False)  # Hide cancel button

    def on_cancel_clicked(self):
        """Handle Cancel button click."""
        if self.generation_thread and self.generation_thread.isRunning():
            self.error_display.append("<span style='color: #f48771;'>Cancelling generation...</span>")
            # Set cancellation flag and request thread interruption
            if self.generation_worker:
                self.generation_worker.cancel()
            self.generation_thread.requestInterruption()
            # Give it a moment to stop gracefully
            if not self.generation_thread.wait(1000):  # Wait up to 1 second
                # Force terminate if it doesn't stop
                self.generation_thread.terminate()
                # Do not wait indefinitely here to avoid blocking the GUI

    def on_save_clicked(self):
        """Handle Save button click."""
        if not self.current_file:
            # If no file is open, prompt for save location
            file_name, _ = QFileDialog.getSaveFileName(
                self,
                "Save Configuration",
                "",
                "VAST Files (*.vast);;YAML Files (*.yaml *.yml);;All Files (*)"
            )
            if file_name:
                if self.save_to_file(file_name):
                    self.current_file = file_name
                    self.setWindowTitle(f"Configuration Editor - {Path(file_name).name}")
                    self.error_display.append(f"<span style='color: #4ec9b0;'>✓ Saved to {Path(file_name).name}</span>")
        else:
            # Save to the original vast file
            if self.save_to_file(self.current_file):
                self.error_display.append(f"<span style='color: #4ec9b0;'>✓ Saved to {Path(self.current_file).name}</span>")

    def on_save_as_clicked(self):
        """Handle Save As button click - always prompt for a new filename."""
        # Prompt for save location
        file_name, _ = QFileDialog.getSaveFileName(
            self,
            "Save Configuration As",
            self.current_file or "",
            "VAST Files (*.vast);;YAML Files (*.yaml *.yml);;All Files (*)"
        )
        if file_name:
            if self.save_to_file(file_name):
                self.current_file = file_name
                self.setWindowTitle(f"Configuration Editor - {Path(file_name).name}")
                self.error_display.append(f"<span style='color: #4ec9b0;'>✓ Saved to {Path(file_name).name}</span>")

    def on_generation_progress(self, message):
        """Handle progress updates from the worker."""
        self.error_display.append(f"<span style='color: #dcdcaa;'>{message}</span>")

    def on_generation_finished(self):
        """Handle successful completion of generation."""
        configs = self.generation_worker.configs
        variation_gui_classes = self.generation_worker.variation_gui_classes
        # Display success message
        self.error_display.append(f"<span style='color: #4ec9b0;'><b>✓ Generated {len(configs)} config(s)!</b></span>")

        # Update config list - store full config data
        config_list_data = []
        for i, config in enumerate(configs):
            config_list_data.append({
                "name": config.get("name", f"config_{i}"),
                "status": "Generated",
                "data": config  # Store the full config data
            })
        self.config_list.update_configs(config_list_data)
        self.config_view.update_configs(variation_gui_classes, self.temp_dir.name)
        # Auto-select the first config if any configs were generated
        if len(config_list_data) > 0:
            self.config_list.select_config(0)

    def on_generation_error(self, error_message):
        """Handle generation errors."""
        error_msg = f"<span style='color: #f48771;'><b>Generation Error:</b></span><br>"
        error_msg += f"<span style='color: #dcdcaa;'>{error_message}</span>"
        self.error_display.append(error_msg)

    def on_generation_cancelled(self):
        """Handle generation cancellation."""
        self.error_display.append("<span style='color: #f48771;'><b>✗ Generation cancelled</b></span>")

    def cleanup_generation(self):

        if self.temp_config:
            del self.temp_config
        if self.temp_dir:
            del self.temp_dir

    def validate_config(self):
        """Validate the YAML configuration."""
        yaml_text = self.editor.toPlainText().strip()

        if not yaml_text:
            self.error_display.clear()
            return

        # First, try to parse YAML
        try:
            config_data = self.yaml_parser.load(yaml_text)
        except YAMLError as e:
            error_msg = f"<span style='color: #f48771;'><b>YAML Syntax Error:</b></span><br>"
            error_msg += f"<span style='color: #dcdcaa;'>{str(e)}</span>"
            self.error_display.setHtml(error_msg)
            return
        except Exception as e:
            error_msg = f"<span style='color: #f48771;'><b>Parsing Error:</b></span><br>"
            error_msg += f"<span style='color: #dcdcaa;'>{str(e)}</span>"
            self.error_display.setHtml(error_msg)
            return

        # Then, validate against Pydantic schema
        try:
            validate_config(config_data)
            success_msg = "<span style='color: #4ec9b0;'><b>✓ Configuration is valid!</b></span><br>"
            success_msg += f"<span style='color: #9cdcfe;'>Version: {config_data.get('version', 'N/A')}</span>"
            self.error_display.setHtml(success_msg)
        except ValueError as e:
            error_msg = f"<span style='color: #f48771;'><b>Validation Error:</b></span><br>"
            error_msg += f"<span style='color: #dcdcaa;'>{str(e)}</span>"
            self.error_display.setHtml(error_msg)
        except Exception as e:
            error_msg = f"<span style='color: #f48771;'><b>Unexpected Error:</b></span><br>"
            error_msg += f"<span style='color: #dcdcaa;'>{str(e)}</span>"
            self.error_display.setHtml(error_msg)

    def keyPressEvent(self, event: QKeyEvent):
        """Handle key press events."""
        # Check for Ctrl+S to save
        # Ctrl+S => Save
        if event.key() == Qt.Key_S and event.modifiers() == Qt.ControlModifier:
            self.on_save_clicked()
            event.accept()
        # Ctrl+Shift+S => Save As
        elif event.key() == Qt.Key_S and (event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)) == (Qt.ControlModifier | Qt.ShiftModifier):
            self.on_save_as_clicked()
            event.accept()
        else:
            # Pass other key events to the base class
            super().keyPressEvent(event)


def main():
    """Main entry point for the application."""
    app = QApplication(sys.argv)
    app.setApplicationName("YAML Config Editor")

    window = ConfigEditor(None)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
