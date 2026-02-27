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


def _scrollbar_css(theme: str) -> str:
    if theme == 'dark':
        track = "rgba(255, 255, 255, 0.08)"
        thumb = "rgba(255, 255, 255, 0.25)"
        thumb_hover = "rgba(255, 255, 255, 0.35)"
        color_scheme = "dark"
    else:
        track = "rgba(0, 0, 0, 0.05)"
        thumb = "rgba(0, 0, 0, 0.25)"
        thumb_hover = "rgba(0, 0, 0, 0.35)"
        color_scheme = "light"

    return f"""
<style id="robovast-scrollbar-style">
  html {{
    font-size: 14px;
    color-scheme: {color_scheme};
  }}
  :root {{
    --rv-scrollbar-track: {track};
    --rv-scrollbar-thumb: {thumb};
    --rv-scrollbar-thumb-hover: {thumb_hover};
  }}
  * {{
    scrollbar-width: thin; /* Firefox */
    scrollbar-color: var(--rv-scrollbar-thumb) var(--rv-scrollbar-track); /* Firefox */
  }}
  *::-webkit-scrollbar {{
    width: 12px;
    height: 12px;
  }}
  *::-webkit-scrollbar-track {{
    background: var(--rv-scrollbar-track);
  }}
  *::-webkit-scrollbar-thumb {{
    background-color: var(--rv-scrollbar-thumb);
    border-radius: 8px;
    border: 3px solid transparent;
    background-clip: content-box;
  }}
  *::-webkit-scrollbar-thumb:hover {{
    background-color: var(--rv-scrollbar-thumb-hover);
  }}
</style>
""".strip()


def _inject_css_into_html_head(html_text: str, css_block: str) -> str:
    if not html_text or not css_block:
        return html_text

    if 'id="robovast-scrollbar-style"' in html_text:
        return html_text

    head_close = re.search(r"</head\s*>", html_text, flags=re.IGNORECASE)
    if head_close:
        idx = head_close.start()
        return html_text[:idx] + "\n" + css_block + "\n" + html_text[idx:]

    head_open = re.search(r"<head(\s+[^>]*)?>", html_text, flags=re.IGNORECASE)
    if head_open:
        idx = head_open.end()
        return html_text[:idx] + "\n" + css_block + "\n" + html_text[idx:]

    return css_block + "\n" + html_text


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
        stripped = line.strip()

        if "Traceback" in (stripped if stripped else line):
            in_traceback = True
            continue

        if in_traceback:
            if stripped and any(err_type in stripped for err_type in ['Error:', 'Exception:', 'AttributeError:', 'NameError:', 'TypeError:', 'ValueError:', 'ImportError:', 'KeyError:', 'IndexError:']):
                if ':' in stripped:
                    error_type = stripped.split(':')[0].strip()
                    error_message = ':'.join(stripped.split(':')[1:]).strip()
                else:
                    error_type = stripped
                break
            # Skip redundant "Cell In[X], line Y" - we show this in the header
            if stripped and re.search(r'Cell In\[\d+\], line \d+', stripped):
                continue
            traceback_lines.append(line)  # Preserve original for newlines and indentation

    # If no specific error was found, use the last non-empty line
    if not error_message and lines:
        last_line = [line for line in lines if line.strip()][-1] if lines else ""
        if ':' in last_line:
            parts = last_line.split(':', 1)
            error_type = parts[0].strip()
            error_message = parts[1].strip()
        else:
            error_message = last_line

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
            font-size: 14px;
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
            padding: 20px;
        }}

        .cell-source {{
            background: #f8f9fa;
            border-left: 4px solid #6c757d;
            padding: 14px 16px;
            margin-bottom: 16px;
            border-radius: 0 4px 4px 0;
        }}

        .cell-source-header {{
            color: #495057;
            margin: 0 0 10px 0;
            font-size: 0.85rem;
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
            font-size: 0.8rem;
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
            background: #fff5f5;
            border-left: 4px solid #dc3545;
            padding: 16px 20px;
            margin-bottom: 16px;
            border-radius: 0 6px 6px 0;
        }}

        .error-type h3 {{
            color: #dc3545;
            margin: 0 0 6px 0;
            font-size: 0.9rem;
            font-weight: 600;
        }}

        .error-message {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            color: #333;
            line-height: 1.5;
        }}

        .traceback-content {{
            background: #2c3e50;
            color: #ecf0f1;
            padding: 12px 15px;
            border-radius: 4px;
            font-family: 'Courier New', Consolas, monospace;
            font-size: 0.75rem;
            line-height: 1.5;
            overflow-x: auto;
            max-height: 200px;
            overflow-y: auto;
            margin: 12px 0 0 0;
            white-space: pre;
            word-break: normal;
        }}

        .traceback-label {{
            color: #6c757d;
            font-size: 0.8rem;
            margin-bottom: 6px;
        }}
    </style>
</head>
<body>
    <div class="error-container">
        <div class="error-content">
            <div class="error-type">
                <div class="error-message">🚨 {error_type}:{error_message or 'No detailed error message available.'}</div>
            </div>

            {f'<pre class="traceback-content">' + html.escape('\n'.join(traceback_lines)) + '</pre>' if traceback_lines else ''}
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
        path = self._get_external_notebook_path(run_type)
        if path:
            hash_files = [os.path.abspath(path)]
            return hash_files
        return []

    def run(self, data_path, run_type):
        if not self._get_external_notebook_path(run_type):
            return False, "Notebook not available"
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
                # Export only successfully executed cells (exclude failing cell and later)
                error_str = str(e)
                cell_match = re.search(r'Error in cell (\d+) of \d+:', error_str)
                if cell_match:
                    failing_cell_1based = int(cell_match.group(1))
                    # Keep only cells before the failing one (0-based index)
                    truncate_at = failing_cell_1based - 1
                    nb_copy = nbformat.v4.new_notebook(metadata=notebook.metadata)
                    nb_copy.cells = list(notebook.cells[:truncate_at])
                else:
                    # Cannot determine failing cell; show only the error
                    nb_copy = None

                error_html = format_notebook_error_html(str(e))
                error_body_match = re.search(
                    r'<body[^>]*>(.*?)</body>', error_html, re.DOTALL | re.IGNORECASE)
                error_content = (
                    error_body_match.group(1).strip() if error_body_match else error_html)
                error_banner = (
                    '<div class="error-banner" style="background: #dc3545; color: white; '
                    'padding: 10px 16px; margin: 16px 0; border-radius: 4px; font-size: 13px; font-weight: 600;">'
                    'Execution stopped due to an error in a cell. Output above shows completed cells.</div>')

                if nb_copy and nb_copy.cells:
                    html_exporter = HTMLExporter()
                    html_exporter.template_name = 'lab'
                    html_exporter.theme = detect_theme()
                    try:
                        html_exporter.exclude_input = True
                        html_exporter.exclude_input_prompt = True
                        html_exporter.exclude_output_prompt = False
                    except Exception:
                        pass

                    (partial_body, _) = html_exporter.from_notebook_node(nb_copy)
                    partial_body = _inject_css_into_html_head(
                        partial_body, _scrollbar_css(html_exporter.theme))
                    combined_html = partial_body.replace(
                        '</body>', f'\n{error_banner}\n<div style="margin-top: 1em;">{error_content}</div>\n</body>')
                else:
                    combined_html = error_html

                with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as temp_file:
                    temp_file.write(combined_html)
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
            body = _inject_css_into_html_head(body, _scrollbar_css(html_exporter.theme))

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
            font-size: 12px;
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
                    font-size: 14px;
                    -webkit-font-smoothing: antialiased;
                    -moz-osx-font-smoothing: grayscale;
                }}
            </style>
            {_scrollbar_css(theme)}
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
                    font-size: 14px;
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
                    font-size: 18px;
                    font-weight: bold;
                    margin-bottom: 10px;
                    color: {border_color};
                }}
                .error-message {{
                    font-size: 14px;
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
            {_scrollbar_css(theme)}
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

        if html_file and os.path.exists(html_file):
            # Load HTML file directly using setUrl
            file_url = QUrl.fromLocalFile(html_file)
            self.web_view.setUrl(file_url)
        else:
            # Handle error or missing file
            theme = detect_theme()
            if theme == 'dark':
                bg = '#121212'
                fg = '#e0e0e0'
            else:
                bg = '#ffffff'
                fg = '#111111'

            error_html = f"""
            <html>
            <body style="font-family: sans-serif; font-size: 14px; text-align: center; padding-top: 50px; background-color: {bg}; color: {fg};">
                <h4 style="font-size: 16px;">{html_file if html_file else "Analysis not available"}</h4>
                <p style="font-size: 12px;">The requested notebook analysis is not defined.</p>
            </body>
            </html>
            """
            self.web_view.setHtml(error_html)
            self.loading_overlay.hide_loading()

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
