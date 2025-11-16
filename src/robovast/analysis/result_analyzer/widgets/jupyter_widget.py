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

import html
import os
import re
import tempfile

import nbformat
from nbconvert import HTMLExporter
from nbconvert.preprocessors import ExecutePreprocessor
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QPalette
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QApplication, QFrame, QLabel, QProgressBar,
                               QVBoxLayout, QWidget)

from robovast.common import FileCache

from .common import RunType
from .worker_thread import CancellableWorkload


def detect_theme() -> str:
    """Detect if the application is using dark or light theme"""
    try:
        app = QApplication.instance()
        if app is None:
            return 'light'  # Default to light if no app instance

        # Get the application's palette
        palette = app.palette()

        # Compare window background color brightness
        # Qt6 uses QPalette.ColorRole instead of direct attributes
        window_color = palette.color(QPalette.ColorRole.Window)
        # Calculate brightness using luminance formula
        brightness = (0.299 * window_color.red() +
                      0.587 * window_color.green() +
                      0.114 * window_color.blue())

        # If brightness is less than 128 (out of 255), it's likely a dark theme
        return 'dark' if brightness < 128 else 'light'
    except Exception:
        # If anything goes wrong, default to light theme
        return 'light'


def clean_ansi_codes(text: str) -> str:
    """Remove ANSI color codes and escape sequences from text"""
    # Remove ANSI color codes like [0;31m, [0m, etc.
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)


def format_notebook_error_html(error_str: str) -> str:
    """Format notebook execution errors as a nice-looking HTML page"""
    # Clean ANSI codes first
    clean_error = clean_ansi_codes(error_str)

    # Extract the main error type and message
    lines = clean_error.split('\n')

    # Look for the actual error type (AttributeError, NameError, etc.)
    error_type = "Execution Error"
    error_message = ""
    traceback_lines = []
    cell_info = None
    cell_source = None
    error_line_number = None

    # Check if error message contains cell information
    cell_match = re.search(r'Error in cell (\d+) of (\d+):', clean_error)
    if cell_match:
        cell_num = cell_match.group(1)
        total_cells = cell_match.group(2)
        cell_info = f"Cell {cell_num} of {total_cells}"

    # Extract cell source code if present
    source_match = re.search(r'--- Cell Source ---\n(.*?)\n--- End Cell Source ---', clean_error, re.DOTALL)
    if source_match:
        cell_source = source_match.group(1).strip()

    # Try to extract the line number from the traceback
    # Look for patterns like "line 5" or "<ipython-input-X>, line 5"
    line_match = re.search(r'line (\d+)', clean_error)
    if line_match:
        error_line_number = int(line_match.group(1))

    # Parse the error to extract meaningful information
    in_traceback = False
    for line in lines:
        line = line.strip()
        if not line:
            continue

        if "Traceback" in line:
            in_traceback = True
            continue

        if in_traceback:
            if any(err_type in line for err_type in ['Error:', 'Exception:', 'AttributeError:', 'NameError:', 'TypeError:', 'ValueError:', 'ImportError:', 'KeyError:', 'IndexError:']):
                if ':' in line:
                    error_type = line.split(':')[0].strip()
                    error_message = ':'.join(line.split(':')[1:]).strip()
                else:
                    error_type = line
                break
            else:
                traceback_lines.append(line)

    # If no specific error was found, use the last non-empty line
    if not error_message and lines:
        last_line = [line for line in lines if line.strip()][-1] if lines else ""
        if ':' in last_line:
            parts = last_line.split(':', 1)
            error_type = parts[0].strip()
            error_message = parts[1].strip()
        else:
            error_message = last_line

    # Create a clean HTML error page
    # Escape HTML in cell source and preserve formatting
    cell_source_html = ""
    if cell_source:
        # Split into lines and add line numbers with highlighting
        source_lines = cell_source.split('\n')
        formatted_lines = []

        # Determine the range of lines to show (max 10 lines before error)
        if error_line_number:
            start_line = max(1, error_line_number - 10)
        else:
            start_line = 1

        for i, line in enumerate(source_lines, start=1):
            # Skip lines before start_line
            if i < start_line:
                continue

            escaped_line = html.escape(line)
            # Highlight the error line if we know which one it is
            if error_line_number and i == error_line_number:
                formatted_lines.append(
                    f'<span class="line-number error-line-number">{i:3d}</span>'
                    f'<span class="error-line">{escaped_line}</span>'
                )
                # Stop displaying lines after the error line
                break
            else:
                formatted_lines.append(
                    f'<span class="line-number">{i:3d}</span>'
                    f'<span class="code-line">{escaped_line}</span>'
                )

        formatted_code = '\n'.join(formatted_lines)

        # Build header with cell info and error line number
        header_parts = []
        if cell_info:
            header_parts.append(f'<strong>📍 {cell_info}</strong>')
        if error_line_number:
            header_parts.append(f'<span style="color: #dc3545; font-weight: bold;">Error on line {error_line_number}</span>')

        header = ' - '.join(header_parts) if header_parts else '💻 Failing Cell Code'

        cell_source_html = f"""
            <div class="cell-source">
                <h4>{header}</h4>
                <pre><code>{formatted_code}</code></pre>
            </div>
        """

    html_template = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Notebook Execution Error</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f8f9fa;
            padding: 20px;
            margin: 0;
        }}

        .error-container {{
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            max-width: 1200px;
            margin: 0 auto;
        }}

        .error-content {{
            padding: 30px;
        }}

        .cell-source {{
            background: #f8f9fa;
            border-left: 4px solid #6c757d;
            padding: 20px;
            margin-bottom: 20px;
        }}

        .cell-source h4 {{
            color: #495057;
            margin: 0 0 15px 0;
            font-size: 1.1rem;
        }}

        .cell-source pre {{
            background: #2c3e50;
            color: #ecf0f1;
            padding: 15px;
            border-radius: 4px;
            overflow-x: auto;
            margin: 0;
        }}

        .cell-source code {{
            font-family: 'Courier New', 'Consolas', monospace;
            font-size: 0.9rem;
            line-height: 1.6;
            display: block;
        }}

        .line-number {{
            display: inline-block;
            width: 3em;
            text-align: right;
            margin-right: 1em;
            color: #7f8c8d;
            user-select: none;
            border-right: 2px solid #34495e;
            padding-right: 0.5em;
        }}

        .error-line-number {{
            background: #c0392b;
            color: #fff;
            font-weight: bold;
            border-right: 2px solid #e74c3c;
        }}

        .code-line {{
            display: inline;
        }}

        .error-line {{
            display: inline;
            background: #e74c3c;
            color: #fff;
            padding: 2px 4px;
            margin-left: -4px;
            font-weight: bold;
        }}

        .error-type {{
            background: #f8f9fa;
            border-left: 4px solid #dc3545;
            padding: 20px;
            margin-bottom: 20px;
        }}

        .error-type h3 {{
            color: #dc3545;
            margin: 0 0 10px 0;
            font-size: 1.2rem;
        }}

        .error-message {{
            color: #333;
            line-height: 1.5;
        }}

        .traceback-content {{
            background: #2c3e50;
            color: #ecf0f1;
            padding: 15px;
            border-radius: 4px;
            font-family: 'Courier New', monospace;
            font-size: 0.9rem;
            line-height: 1.4;
            overflow-x: auto;
            max-height: 300px;
            overflow-y: auto;
            margin-top: 15px;
        }}
    </style>
</head>
<body>
    <div class="error-container">
        <div class="error-content">
            <div class="error-type">
                <h3>🚨 {error_type}</h3>
                <div class="error-message">{error_message or 'No detailed error message available.'}</div>
            </div>

            {cell_source_html}

            {"<div class='traceback-content'>" + '<br>'.join(traceback_lines) + "</div>" if traceback_lines else ""}
        </div>
    </div>
</body>
</html>
"""

    return html_template


class JupyterNotebookRunner(CancellableWorkload):
    """Thread for executing notebooks without blocking the UI"""

    def __init__(self, name, single_test_nb, config_nb, run_nb):
        super().__init__(name)
        self.notebook_content = None
        self.single_test_nb = single_test_nb
        self.config_nb = config_nb
        self.run_nb = run_nb

    def set_notebook(self, notebook_content: str):
        """Set the notebook content to execute"""
        self.notebook_content = notebook_content

    def get_notebook(self, path, run_type):
        """Load and prepare notebook with DATA_DIR replaced.

        Returns:
            nbformat.NotebookNode: The prepared notebook object
        """
        notebook_content = self._load_external_notebook(run_type)
        if not notebook_content:
            return None

        # Parse the notebook as JSON/nbformat object
        try:
            notebook = nbformat.reads(notebook_content, as_version=4)
        except Exception as e:
            raise ValueError(f"Failed to parse notebook as JSON: {e}") from e

        # Find and replace DATA_DIR in code cells
        replace_variable = "DATA_DIR"
        replace_string = f"'{os.path.abspath(path)}'"
        regex_pattern = re.compile(r'(?m)^(\s*)DATA_DIR\s*=\s*([\'"]).*?\2(.*)$')

        num_replacements = 0
        for cell in notebook.cells:
            if cell.cell_type == 'code':
                # Replace DATA_DIR assignment in this cell
                cell.source, count = regex_pattern.subn(
                    rf'\1{replace_variable} = {replace_string}\3',
                    cell.source
                )
                num_replacements += count

        if num_replacements == 0:
            raise ValueError(f"Expected at least one replacement of '{replace_variable}', but made {num_replacements} replacements.")

        # Return notebook object directly
        return notebook

    def _get_external_notebook_path(self, run_type) -> str:
        if run_type == RunType.SINGLE_TEST:
            return self.single_test_nb
        elif run_type == RunType.CONFIG:
            return self.config_nb
        elif run_type == RunType.RUN:
            return self.run_nb
        return None

    def _load_external_notebook(self, run_type) -> str:
        """Load and notebook from external file"""
        notebook_path = self._get_external_notebook_path(run_type)
        if not notebook_path:
            return None

        # # Make path absolute if it's relative
        # if not os.path.isabs(notebook_path):
        #     # Try relative to the script directory first
        #     base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        #     notebook_path = os.path.join(base_dir, notebook_path)

        print(f"Loading notebook from {notebook_path}...")
        try:
            # Load notebook file
            if os.path.exists(notebook_path):
                with open(notebook_path, 'r', encoding='utf-8') as f:
                    notebook_content = f.read()

                return notebook_content
            else:
                print(f"Notebook {notebook_path} does not exist.")
                return None

        except Exception as e:
            print(f"Error loading notebook from {notebook_path}: {e}")
            return None

    def get_cache_file_name(self, run_type):
        """Determine CSV files and analysis type based on directory structure"""
        if run_type == RunType.SINGLE_TEST:
            return "overview_single_test.html"
        elif run_type == RunType.CONFIG:
            return "overview_config.html"
        elif run_type == RunType.RUN:
            return "overview_run.html"
        raise ValueError("Unknown run-type")

    def get_hash_files(self, run_type):
        """Get list of files to hash for caching purposes"""
        hash_files = [os.path.abspath(self._get_external_notebook_path(run_type))]
        return hash_files

    def run(self, data_path, run_type):
        hash_files = self.get_hash_files(run_type)
        cache_file_name = self.get_cache_file_name(run_type)
        file_cache = FileCache(data_path, cache_file_name, hash_files, ".html")
        try:
            # Check cache first
            cached_file = file_cache.get_cached_file(hash_files, None, content=False)
            if cached_file:
                # Use cached HTML
                print("Use cached analysis results.")
                return True, os.path.abspath(cached_file)

            self.progress_callback(0, "Creating notebook...")

            notebook = self.get_notebook(data_path, run_type)
            if not notebook:
                raise ValueError("Failed to prepare notebook content.")

            # Configure the executor
            self.progress_callback(10, "Setting up execution environment...")

            class ProgressExecutePreprocessor(ExecutePreprocessor):
                def __init__(self, progress_callback=None, is_canceled_callback=None, *args, **kwargs):  # pylint: disable=keyword-arg-before-vararg
                    super().__init__(*args, **kwargs)
                    self.current_cell = 0.
                    self.cells = 1
                    self.progress = 0.
                    self.progress_callback = progress_callback
                    self.is_canceled_callback = is_canceled_callback

                def preprocess(self, nb, resources):
                    self.cells = len(nb.cells)
                    return super().preprocess(nb, resources)

                def preprocess_cell(self, cell, resources, index):
                    self.current_cell += 1
                    self.progress = self.current_cell / self.cells
                    if self.progress_callback:
                        progress_value = 20 + int(self.progress * 60)  # Scale from 20% to 80%
                        self.progress_callback(progress_value, f"Executing cell {index + 1}/{self.cells}...")
                    if self.is_canceled_callback and self.is_canceled_callback():
                        raise RuntimeError("Notebook execution canceled by user.")
                    try:
                        return super().preprocess_cell(cell, resources, index)
                    except Exception as e:
                        # Re-raise with cell number and source code information
                        cell_source = cell.get('source', '')
                        error_msg = f"Error in cell {
                            index + 1} of {self.cells}: {str(e)}\n\n--- Cell Source ---\n{cell_source}\n--- End Cell Source ---"
                        raise RuntimeError(error_msg) from e

            executor = ProgressExecutePreprocessor(progress_callback=self.progress_callback,
                                                   is_canceled_callback=self.is_cancelled, timeout=600, kernel_name='python3')
            try:
                executor.preprocess(notebook, {'metadata': {'path': os.path.dirname(data_path)}})
            except Exception as e:
                error_html = format_notebook_error_html(str(e))
                with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as temp_file:
                    temp_file.write(error_html)
                    temp_file_path = temp_file.name
                return False, temp_file_path
            self.progress_callback(90, "Converting to HTML...")

            html_exporter = HTMLExporter()
            html_exporter.template_name = 'lab'  # Use 'lab' template for full-featured HTML
            html_exporter.theme = detect_theme()
            # Hide code cell inputs in the exported HTML
            try:
                html_exporter.exclude_input = True
                html_exporter.exclude_input_prompt = True
                # Also hide raw input prompt decorations if supported
                html_exporter.exclude_output_prompt = False
            except Exception:
                # Older nbconvert versions may not support these attributes
                pass

            (body, _) = html_exporter.from_notebook_node(notebook)

            # Write the HTML content to the cache file
            with open(cache_file_name, 'w', encoding='utf-8') as cache_file:
                cache_file.write(body)

            cache_file_path = file_cache.save_file_to_cache(input_files=hash_files, file_content=body)

            self.progress_callback(100, "Notebook execution completed!")
            return True, os.path.abspath(cache_file_path)

        except Exception as e:
            # Clean up the error message and create a nice HTML error page
            error_html = format_notebook_error_html(str(e))
            with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as temp_file:
                temp_file.write(error_html)
                temp_file_path = temp_file.name
            return False, temp_file_path


class LoadingOverlay(QFrame):
    """Semi-transparent overlay with progress bar for loading states"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setStyleSheet("""
            LoadingOverlay {
                background-color: rgba(0, 0, 0, 150);
                border-radius: 10px;
            }
        """)

        # Create layout
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        # Loading message
        self.message_label = QLabel("Loading content...")
        self.message_label.setStyleSheet("""
            color: white;
            font-size: 14px;
            font-weight: bold;
            padding: 10px;
        """)
        self.message_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.message_label)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 2px solid white;
                border-radius: 5px;
                background-color: rgba(255, 255, 255, 50);
                text-align: center;
                color: white;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background-color: #4CAF50;
                border-radius: 3px;
            }
        """)
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(25)
        self.progress_bar.setFixedWidth(300)
        layout.addWidget(self.progress_bar)

        self.hide()

    def show_loading(self, message: str = "Loading content..."):
        """Show the overlay with a loading message"""
        self.message_label.setText(message)
        self.progress_bar.setValue(0)
        self.show()
        self.raise_()

    def update_progress(self, value: int, message: str = None):
        """Update progress value and optionally message"""
        self.progress_bar.show()
        self.progress_bar.setValue(value)
        if message:
            self.message_label.setText(message)

    def update_progress_no_loading(self, message: str = None):
        """Update progress value and optionally message"""
        self.progress_bar.hide()
        if message:
            self.message_label.setText(message)

    def hide_loading(self):
        """Hide the loading overlay"""
        self.hide()

    def resizeEvent(self, event):
        """Ensure overlay covers the entire parent widget"""
        super().resizeEvent(event)
        if self.parent():
            self.setGeometry(self.parent().rect())


class DataAnalysisWidget(QWidget):
    """Main widget for web-based Jupyter notebook data analysis"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_data_directory = None
        self.current_analysis_type = None

        # Caching variables
        self.current_csv_hash = None
        self.current_csv_files = []

        self.init_ui()
        self.setup_loading_connections()

    def init_ui(self):
        """Initialize the user interface"""
        layout = QVBoxLayout(self)
        # Determine current theme and prepare welcome HTML accordingly
        theme = detect_theme()

        # Web view for notebook display (maximized)
        self.web_view = QWebEngineView()

        # Configure web view settings for proper HTML rendering
        settings = self.web_view.settings()
        settings.setAttribute(settings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(settings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(settings.WebAttribute.JavascriptEnabled, True)

        # Set an initial HTML that matches the current application theme to avoid
        # showing a white flash when the app uses a dark palette.
        self.web_view.setHtml(self.get_welcome_html(theme))
        layout.addWidget(self.web_view, 1)  # Give stretch factor of 1 to maximize

        # Create loading overlay
        self.loading_overlay = LoadingOverlay(self)

    def setup_loading_connections(self):
        """Setup connections for webview loading events"""
        self.web_view.loadStarted.connect(self.on_load_started)
        self.web_view.loadProgress.connect(self.on_load_progress)
        self.web_view.loadFinished.connect(self.on_load_finished)

    def on_load_started(self):
        """Called when webview starts loading"""
        self.loading_overlay.show_loading("Loading content...")

    def on_load_progress(self, progress):
        """Called during webview loading with progress percentage"""
        self.loading_overlay.update_progress(progress, f"Loading content... {progress}%")

    def on_load_finished(self, success):
        """Called when webview finishes loading"""
        self.loading_overlay.hide_loading()
        if not success:
            # Show minimal error page instead of temporary overlay message
            theme = detect_theme()
            error_html = self.get_error_html(theme)
            self.web_view.setHtml(error_html)

    def resizeEvent(self, event):
        """Ensure overlay is repositioned when widget is resized"""
        super().resizeEvent(event)
        if hasattr(self, 'loading_overlay'):
            self.loading_overlay.setGeometry(self.rect())

    def get_welcome_html(self, theme: str = 'light') -> str:
        """Get simple empty HTML content matching the provided theme.

        Args:
                theme: 'light' or 'dark' to choose appropriate background/colors.
        """
        if theme == 'dark':
            bg = '#121212'
            fg = '#e0e0e0'
        else:
            bg = '#ffffff'
            fg = '#111111'

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset=\"utf-8\" />
            <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
            <style>
                html, body {{ height: 100%; margin: 0; padding: 0; }}
                body {{
                    background-color: {bg};
                    color: {fg};
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    -webkit-font-smoothing: antialiased;
                    -moz-osx-font-smoothing: grayscale;
                }}
            </style>
        </head>
        <body>
        </body>
        </html>
        """

    def get_error_html(self, theme: str = 'light') -> str:
        """Get minimal error page HTML matching the provided theme.

        Args:
                theme: 'light' or 'dark' to choose appropriate background/colors.
        """
        if theme == 'dark':
            bg = '#121212'
            fg = '#e0e0e0'
            border_color = '#dc3545'
            error_bg = '#2c1a1a'
        else:
            bg = '#ffffff'
            fg = '#111111'
            border_color = '#dc3545'
            error_bg = '#f8d7da'

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset=\"utf-8\" />
            <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
            <style>
                html, body {{ height: 100%; margin: 0; padding: 0; }}
                body {{
                    background-color: {bg};
                    color: {fg};
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    -webkit-font-smoothing: antialiased;
                    -moz-osx-font-smoothing: grayscale;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }}
                .error-container {{
                    max-width: 600px;
                    padding: 40px;
                    text-align: center;
                }}
                .error-title {{
                    font-size: 24px;
                    font-weight: bold;
                    margin-bottom: 10px;
                    color: {border_color};
                }}
                .error-message {{
                    font-size: 16px;
                    line-height: 1.5;
                    color: {fg};
                    opacity: 0.8;
                }}
                .error-box {{
                    background-color: {error_bg};
                    border-left: 4px solid {border_color};
                    padding: 20px;
                    margin-top: 20px;
                    border-radius: 4px;
                }}
            </style>
        </head>
        <body>
            <div class="error-container">
                <div class="error-box">
                    <div class="error-title">Failed to Load Content</div>
                    <div class="error-message">
                        The requested content could not be loaded.
                    </div>
                </div>
            </div>
        </body>
        </html>
        """

    def display_html(self, html_file: str):
        """Load HTML file in web view with loading overlay"""
        # Show loading overlay with custom message
        self.loading_overlay.show_loading("Loading analysis results...")

        # Ensure web view is visible
        self.web_view.show()

        # Load HTML file directly using setUrl
        file_url = QUrl.fromLocalFile(html_file)
        self.web_view.setUrl(file_url)

    def clear_output(self):
        """Clear the current output - show empty widget without triggering webview signals"""
        # Hide the webview to show empty widget state without loading signals
        self.web_view.hide()
        # Ensure loading overlay is also hidden
        self.loading_overlay.hide_loading()

    def show_execution_progress(self, progress: int, message: str):
        """Show execution progress overlay (for external use)"""
        self.loading_overlay.show_loading()
        self.loading_overlay.update_progress(progress, message)

    def show_execution_no_progress(self, message: str):
        """Show execution progress overlay (for external use)"""
        self.loading_overlay.show_loading()
        self.loading_overlay.update_progress_no_loading(message)

    def hide_execution_progress(self):
        """Hide execution progress overlay (for external use)"""
        self.loading_overlay.hide_loading()
