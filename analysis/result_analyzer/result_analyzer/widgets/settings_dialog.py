#!/usr/bin/env python3
"""
Settings Dialog for Test Results Analyzer
"""

import os

from PySide2.QtCore import QSettings, Qt
from PySide2.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLineEdit, QPushButton, QSlider,
                               QSpinBox, QTabWidget, QTextEdit, QVBoxLayout,
                               QWidget)

from .config_loader import get_config


class SettingsDialog(QDialog):
    def __init__(self, parent=None, config=None):
        super().__init__(parent)
        self.setWindowTitle("Test Results Analyzer - Settings")
        self.setModal(True)
        self.resize(600, 400)

        # Store references to both settings systems
        # QSettings for local settings (window state, etc.)
        self.settings = QSettings("TestResultsAnalyzer", "Settings")

        # Config loader for shared settings
        self.config = config
        if self.config is None:
            self.config = get_config()

        self.setup_ui()
        self.load_settings()

    def setup_ui(self):
        """Setup the settings dialog UI"""
        layout = QVBoxLayout(self)

        # Tab widget for different setting categories
        self.tab_widget = QTabWidget()
        layout.addWidget(self.tab_widget)

        # General settings tab
        self.setup_general_tab()

        # UI settings tab
        self.setup_ui_tab()

        # Advanced settings tab
        self.setup_advanced_tab()

        # Notebook settings tab
        self.setup_notebook_tab()

        # Directories settings tab
        self.setup_directories_tab()

        # Dialog buttons
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Apply)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        button_box.button(QDialogButtonBox.Apply).clicked.connect(self.apply_settings)
        layout.addWidget(button_box)

    def setup_general_tab(self):
        """Setup the general settings tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Application settings group
        app_group = QGroupBox("Application Settings")
        app_layout = QFormLayout(app_group)

        # Example settings - will be expanded later
        self.auto_expand_tree = QCheckBox("Auto-expand tree on load")
        app_layout.addRow("Tree View:", self.auto_expand_tree)

        self.remember_window_state = QCheckBox("Remember window position and size")
        app_layout.addRow("Window:", self.remember_window_state)

        self.auto_play_video = QCheckBox("Auto-play videos when selected")
        app_layout.addRow("Video:", self.auto_play_video)

        layout.addWidget(app_group)

        # File handling group
        file_group = QGroupBox("File Handling")
        file_layout = QFormLayout(file_group)

        self.max_file_size_mb = QSpinBox()
        self.max_file_size_mb.setRange(1, 1000)
        self.max_file_size_mb.setValue(50)
        self.max_file_size_mb.setSuffix(" MB")
        file_layout.addRow("Max file size for preview:", self.max_file_size_mb)

        layout.addWidget(file_group)

        layout.addStretch()
        self.tab_widget.addTab(tab, "General")

    def setup_ui_tab(self):
        """Setup the UI settings tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Theme and appearance group
        theme_group = QGroupBox("Theme and Appearance")
        theme_layout = QFormLayout(theme_group)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["System Default", "Light", "Dark"])
        theme_layout.addRow("Theme:", self.theme_combo)

        self.font_size = QSpinBox()
        self.font_size.setRange(8, 24)
        self.font_size.setValue(9)
        theme_layout.addRow("Font size:", self.font_size)

        layout.addWidget(theme_group)

        # Video player group
        video_group = QGroupBox("Video Player")
        video_layout = QFormLayout(video_group)

        self.video_controls_always_visible = QCheckBox("Always show video controls")
        video_layout.addRow("Controls:", self.video_controls_always_visible)

        self.video_volume = QSlider(Qt.Horizontal)
        self.video_volume.setRange(0, 100)
        self.video_volume.setValue(50)
        video_layout.addRow("Default volume:", self.video_volume)

        layout.addWidget(video_group)

        layout.addStretch()
        self.tab_widget.addTab(tab, "User Interface")

    def setup_advanced_tab(self):
        """Setup the advanced settings tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Performance group
        perf_group = QGroupBox("Performance")
        perf_layout = QFormLayout(perf_group)

        self.max_tree_depth = QSpinBox()
        self.max_tree_depth.setRange(1, 10)
        self.max_tree_depth.setValue(3)
        perf_layout.addRow("Max tree depth:", self.max_tree_depth)

        self.enable_thumbnails = QCheckBox("Enable video thumbnails")
        perf_layout.addRow("Thumbnails:", self.enable_thumbnails)

        layout.addWidget(perf_group)

        # Debug group
        debug_group = QGroupBox("Debug and Logging")
        debug_layout = QFormLayout(debug_group)

        self.log_level = QComboBox()
        self.log_level.addItems(["ERROR", "WARNING", "INFO", "DEBUG"])
        debug_layout.addRow("Log level:", self.log_level)

        self.enable_debug_output = QCheckBox("Enable debug output")
        debug_layout.addRow("Debug:", self.enable_debug_output)

        layout.addWidget(debug_group)

        layout.addStretch()
        self.tab_widget.addTab(tab, "Advanced")

    def setup_notebook_tab(self):
        """Setup the notebook settings tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Notebook templates group
        notebook_group = QGroupBox("Notebook Templates")
        notebook_layout = QFormLayout(notebook_group)

        # Single run notebook setting
        analysis_single_test_widget = QWidget()
        analysis_single_test_layout = QHBoxLayout(analysis_single_test_widget)
        analysis_single_test_layout.setContentsMargins(0, 0, 0, 0)

        self.analysis_single_test_notebook = QLineEdit()
        self.analysis_single_test_notebook.setPlaceholderText("Path to single run analysis notebook")
        analysis_single_test_layout.addWidget(self.analysis_single_test_notebook)

        analysis_single_test_browse = QPushButton("Browse...")
        analysis_single_test_browse.clicked.connect(lambda: self.browse_notebook_file(self.analysis_single_test_notebook))
        analysis_single_test_layout.addWidget(analysis_single_test_browse)

        notebook_layout.addRow("Single run notebook:", analysis_single_test_widget)

        # Folder runs notebook setting
        analysis_single_variant_widget = QWidget()
        analysis_single_variant_layout = QHBoxLayout(analysis_single_variant_widget)
        analysis_single_variant_layout.setContentsMargins(0, 0, 0, 0)

        self.analysis_single_variant_notebook = QLineEdit()
        self.analysis_single_variant_notebook.setPlaceholderText("Path to folder runs analysis notebook")
        analysis_single_variant_layout.addWidget(self.analysis_single_variant_notebook)

        analysis_single_variant_browse = QPushButton("Browse...")
        analysis_single_variant_browse.clicked.connect(lambda: self.browse_notebook_file(self.analysis_single_variant_notebook))
        analysis_single_variant_layout.addWidget(analysis_single_variant_browse)

        notebook_layout.addRow("Folder runs notebook:", analysis_single_variant_widget)

        # Whole runs notebook setting
        analysis_run_widget = QWidget()
        analysis_run_layout = QHBoxLayout(analysis_run_widget)
        analysis_run_layout.setContentsMargins(0, 0, 0, 0)

        self.analysis_run_notebook = QLineEdit()
        self.analysis_run_notebook.setPlaceholderText("Path to whole runs analysis notebook")
        analysis_run_layout.addWidget(self.analysis_run_notebook)

        analysis_run_browse = QPushButton("Browse...")
        analysis_run_browse.clicked.connect(lambda: self.browse_notebook_file(self.analysis_run_notebook))
        analysis_run_layout.addWidget(analysis_run_browse)

        notebook_layout.addRow("Whole runs notebook:", analysis_run_widget)

        layout.addWidget(notebook_group)

        # Description group
        desc_group = QGroupBox("Notebook Usage")
        desc_layout = QVBoxLayout(desc_group)

        description = QTextEdit()
        description.setReadOnly(True)
        description.setMaximumHeight(120)
        description.setText("""• Single run notebook: Used when analyzing a single ROS bag run
• Folder runs notebook: Used when analyzing a folder containing multiple single runs (e.g., room-generated-p1 with subfolders 0, 1, 2)
• Whole runs notebook: Used for comprehensive analysis across multiple run hierarchies (e.g., runs-1 with multiple scenario folders)

Notebooks should be in Jupyter format (.ipynb) and use {csv_path} as a placeholder for data file paths.""")
        desc_layout.addWidget(description)

        layout.addWidget(desc_group)

        # Command-line execution group
        cmdline_group = QGroupBox("ROSBag to CSV Conversion")
        cmdline_layout = QFormLayout(cmdline_group)

        # Command line setting
        self.external_command = QLineEdit()
        self.external_command.setPlaceholderText("e.g., python analysis/external_analyzer.py")
        cmdline_layout.addRow("Command line:", self.external_command)

        # Description for command line
        cmdline_desc = QTextEdit()
        cmdline_desc.setReadOnly(True)
        cmdline_desc.setMaximumHeight(60)
        cmdline_desc.setText(
            """ROSBag to CSV command to execute for each test run. Two parameters will get appended automatically: --input <rosbag2-path> --output <csv-path>.""")
        cmdline_layout.addRow("Description:", cmdline_desc)

        layout.addWidget(cmdline_group)

        layout.addStretch()
        self.tab_widget.addTab(tab, "Notebooks")

    def setup_directories_tab(self):
        """Setup the directories settings tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Directories group
        dirs_group = QGroupBox("Directory Settings")
        dirs_layout = QFormLayout(dirs_group)

        # Results directory setting
        results_dir_widget = QWidget()
        results_dir_layout = QHBoxLayout(results_dir_widget)
        results_dir_layout.setContentsMargins(0, 0, 0, 0)

        self.results_dir = QLineEdit()
        self.results_dir.setPlaceholderText("downloaded_files")
        results_dir_layout.addWidget(self.results_dir)

        results_dir_browse = QPushButton("Browse...")
        results_dir_browse.clicked.connect(lambda: self.browse_directory(self.results_dir))
        results_dir_layout.addWidget(results_dir_browse)

        dirs_layout.addRow("Results directory:", results_dir_widget)

        layout.addWidget(dirs_group)

        # Description group
        desc_group = QGroupBox("Directory Information")
        desc_layout = QVBoxLayout(desc_group)

        description = QTextEdit()
        description.setReadOnly(True)
        description.setMaximumHeight(80)
        description.setText(
            """• Results directory: Location where test results are stored. This should contain run- directories with test data, videos, and log files.""")
        desc_layout.addWidget(description)

        layout.addWidget(desc_group)

        layout.addStretch()
        self.tab_widget.addTab(tab, "Directories")

    def browse_directory(self, line_edit):
        """Browse for directory"""
        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Directory",
            line_edit.text() or ""
        )
        if directory:
            relative_directory = os.path.relpath(directory, os.getcwd())
            line_edit.setText(relative_directory)

    def browse_notebook_file(self, line_edit):
        """Browse for notebook file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Jupyter Notebook",
            "",
            "Jupyter Notebooks (*.ipynb);;All Files (*)"
        )
        if file_path:
            line_edit.setText(file_path)

    def load_settings(self):
        """Load settings from configuration file"""
        # General settings
        self.auto_expand_tree.setChecked(self.config.get_bool("general", "auto_expand_tree"))
        self.remember_window_state.setChecked(self.config.get_bool("general", "remember_window_state"))
        self.auto_play_video.setChecked(self.config.get_bool("general", "auto_play_video"))
        self.max_file_size_mb.setValue(self.config.get_int("general", "max_file_size_mb"))

        # UI settings
        self.theme_combo.setCurrentText(self.config.get("ui", "theme"))
        self.font_size.setValue(self.config.get_int("ui", "font_size"))
        self.video_controls_always_visible.setChecked(self.config.get_bool("ui", "video_controls_always_visible"))
        self.video_volume.setValue(self.config.get_int("ui", "video_volume"))

        # Advanced settings
        self.max_tree_depth.setValue(self.config.get_int("advanced", "max_tree_depth"))
        self.enable_thumbnails.setChecked(self.config.get_bool("advanced", "enable_thumbnails"))
        self.log_level.setCurrentText(self.config.get("advanced", "log_level"))
        self.enable_debug_output.setChecked(self.config.get_bool("advanced", "enable_debug_output"))

        # Notebook settings
        self.analysis_single_test_notebook.setText(self.config.get("notebooks", "analysis_single_test"))
        self.analysis_single_variant_notebook.setText(self.config.get("notebooks", "analysis_single_variant"))
        self.analysis_run_notebook.setText(self.config.get("notebooks", "analysis_run"))

        # External command settings
        self.external_command.setText(self.config.get("execution", "rosbag_2_csv_conversion_command"))

        # Directory settings
        self.results_dir.setText(self.config.get("directories", "results_dir"))

    def save_settings(self):
        """Save settings to configuration file"""
        # General settings
        self.config.set("general", "auto_expand_tree", self.auto_expand_tree.isChecked())
        self.config.set("general", "remember_window_state", self.remember_window_state.isChecked())
        self.config.set("general", "auto_play_video", self.auto_play_video.isChecked())
        self.config.set("general", "max_file_size_mb", self.max_file_size_mb.value())

        # UI settings
        self.config.set("ui", "theme", self.theme_combo.currentText())
        self.config.set("ui", "font_size", self.font_size.value())
        self.config.set("ui", "video_controls_always_visible", self.video_controls_always_visible.isChecked())
        self.config.set("ui", "video_volume", self.video_volume.value())

        # Advanced settings
        self.config.set("advanced", "max_tree_depth", self.max_tree_depth.value())
        self.config.set("advanced", "enable_thumbnails", self.enable_thumbnails.isChecked())
        self.config.set("advanced", "log_level", self.log_level.currentText())
        self.config.set("advanced", "enable_debug_output", self.enable_debug_output.isChecked())

        # Notebook settings
        self.config.set("notebooks", "analysis_single_test", self.analysis_single_test_notebook.text())
        self.config.set("notebooks", "analysis_single_variant", self.analysis_single_variant_notebook.text())
        self.config.set("notebooks", "analysis_run", self.analysis_run_notebook.text())

        # External command settings
        self.config.set("execution", "rosbag_2_csv_conversion_command", self.external_command.text())

        # Directory settings
        self.config.set("directories", "results_dir", self.results_dir.text())

        # Save to file
        self.config.save_config()

        # Notebook settings
        self.settings.setValue("notebooks/analysis_single_test", self.analysis_single_test_notebook.text())
        self.settings.setValue("notebooks/analysis_single_variant", self.analysis_single_variant_notebook.text())
        self.settings.setValue("notebooks/analysis_run", self.analysis_run_notebook.text())

        # External command settings
        self.settings.setValue("execution/rosbag_2_csv_conversion_command", self.external_command.text())

        # Sync to make sure settings are written to disk
        self.settings.sync()

    def apply_settings(self):
        """Apply settings without closing dialog"""
        self.save_settings()

    def accept(self):
        """Accept dialog and save settings"""
        self.save_settings()
        super().accept()

    @staticmethod
    def get_setting(key, value_type=None):
        """Static method to get a setting value from anywhere in the application"""
        # Import here to avoid circular imports
        from .config_loader import get_config

        config = get_config()
        parts = key.split('/')

        if len(parts) == 2:
            section, option = parts
            if value_type == bool:
                return config.get_bool(section, option)
            elif value_type == int:
                return config.get_int(section, option)
            elif value_type == float:
                return config.get_float(section, option)
            else:
                return config.get(section, option)

        raise ValueError("Key must be in 'section/option' format")

    @staticmethod
    def set_setting(key, value):
        """Static method to set a setting value from anywhere in the application"""
        # Import here to avoid circular imports
        import sys
        from pathlib import Path
        parent_dir = Path(__file__).parent.parent
        if str(parent_dir) not in sys.path:
            sys.path.insert(0, str(parent_dir))
        from config_loader import get_config

        config = get_config()
        parts = key.split('/')

        if len(parts) == 2:
            section, option = parts
            config.set(section, option, value)
            config.save_config()
