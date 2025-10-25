#!/usr/bin/env python3
"""
Chat Widget - A Qt widget for chatting with AI (Ollama or OpenAI) about test results and logs
"""

from typing import List, Optional
import os
import re
import threading
from datetime import datetime
from pathlib import Path

# Try to import requests, handle gracefully if not available
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    print("Warning: requests module not available. Chat functionality will be limited.")
    REQUESTS_AVAILABLE = False
    requests = None
    requests = None

try:
    from PySide2.QtCore import QSettings, QThread, QTimer, Signal
    from PySide2.QtGui import QFont, QTextCursor
    from PySide2.QtWidgets import (QCheckBox, QComboBox, QGroupBox,
                                   QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                                   QProgressBar, QPushButton, QSpinBox,
                                   QTextEdit, QVBoxLayout, QWidget)
    QT_SUPPORT = True
except ImportError:
    print("Error: PySide2 is required for chat_widget.py")
    QT_SUPPORT = False

from .common import filter_nonrelevant_lines, get_scenario_execution_log_file


class OllamaClient:
    """Client for communicating with Ollama API"""

    def __init__(self, base_url="http://localhost:11434"):
        self.base_url = base_url
        if REQUESTS_AVAILABLE:
            self.session = requests.Session()
            self.session.timeout = 30
        else:
            self.session = None

    def list_models(self):
        """List available models"""
        if not REQUESTS_AVAILABLE or not self.session:
            return []

        try:
            response = self.session.get(f"{self.base_url}/api/tags")
            if response.status_code == 200:
                data = response.json()
                return [model['name'] for model in data.get('models', [])]
            return []
        except Exception as e:
            print(f"Error listing models: {e}")
            return []

    def check_connection(self):
        """Check if Ollama is available"""
        if not REQUESTS_AVAILABLE or not self.session:
            return False

        try:
            response = self.session.get(f"{self.base_url}/api/tags", timeout=5)
            return response.status_code == 200
        except Exception:
            return False

    def generate_response(self, model, prompt, context=None, timeout=None):
        """Generate response from Ollama"""
        if not REQUESTS_AVAILABLE or not self.session:
            return "Error: requests module not available", None

        try:
            payload = {
                "model": model,
                "prompt": prompt,
                "stream": False
            }

            if context:
                payload["context"] = context

            # Use provided timeout if set, otherwise default to 120s
            req_timeout = timeout if timeout is not None else 120
            response = self.session.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=req_timeout
            )

            if response.status_code == 200:
                data = response.json()
                return data.get('response', ''), data.get('context', None)
            else:
                return f"Error: HTTP {response.status_code}", None

        except requests.exceptions.Timeout:
            return "Error: Request timed out", None
        except Exception as e:
            return f"Error: {str(e)}", None


class OpenAIClient:
    """Client for communicating with OpenAI API"""

    def __init__(self, api_key: Optional[str] = None, base_url: str = "https://api.openai.com/v1"):
        self.base_url = base_url.rstrip("/")
        # Prefer env var if api_key not provided
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if REQUESTS_AVAILABLE:
            self.session = requests.Session()
        else:
            self.session = None
        self._apply_auth_header()

    def _apply_auth_header(self):
        if self.session is not None:
            # Reset headers (keep default), then set Authorization if we have a key
            self.session.headers.update({})
            if self.api_key:
                self.session.headers.update({
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                })

    def set_api_key(self, api_key: Optional[str]):
        self.api_key = api_key
        self._apply_auth_header()

    def set_base_url(self, base_url: Optional[str]):
        if base_url:
            self.base_url = base_url.rstrip("/")

    def check_connection(self) -> bool:
        """Basic check: can we access the models endpoint with current API key?"""
        if not REQUESTS_AVAILABLE or not self.session or not self.api_key:
            return False
        try:
            resp = self.session.get(f"{self.base_url}/models", timeout=8)
            return resp.status_code == 200
        except Exception:
            return False

    def list_models(self) -> List[str]:
        """List available models (IDs) from OpenAI.
        Returns an empty list if the API key is missing or request fails.
        """
        if not REQUESTS_AVAILABLE or not self.session or not self.api_key:
            return []
        try:
            resp = self.session.get(f"{self.base_url}/models", timeout=15)
            if resp.status_code == 200:
                data = resp.json() or {}
                items = data.get("data", [])
                # Extract id strings
                models = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
                # Prefer most relevant at top if present
                preferred_order = [
                    "gpt-5", "gpt-5-preview", "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"
                ]
                # Sort with preferred first, keep the rest after
                models_sorted = sorted(models, key=lambda x: (preferred_order.index(
                    x) if x in preferred_order else len(preferred_order), x))
                return models_sorted
            return []
        except Exception as e:
            print(f"Error listing OpenAI models: {e}")
            return []

    def generate_response(self, model: str, prompt: str, context=None, timeout: Optional[int] = None):
        """Generate response using Chat Completions API.
        Note: context is ignored here; upstream stores/uses an Ollama-specific token context.
        """
        if not REQUESTS_AVAILABLE or not self.session:
            return "Error: requests module not available", None
        if not self.api_key:
            return "Error: OpenAI API key not set", None
        try:
            messages = [
                {"role": "system", "content": "You are a helpful assistant for analyzing test results and logs."},
                {"role": "user", "content": prompt},
            ]
            payload = {
                "model": model,
                "messages": messages,
                # "temperature": 0.2,
            }
            req_timeout = timeout if timeout is not None else 120
            resp = self.session.post(f"{self.base_url}/chat/completions", json=payload, timeout=req_timeout)
            if resp.status_code == 200:
                data = resp.json() or {}
                choices = data.get("choices", [])
                if choices:
                    message = choices[0].get("message", {})
                    content = message.get("content", "")
                    return content, None  # No token context used for OpenAI path
                return "", None
            else:
                try:
                    err = resp.json()
                except Exception:
                    err = {"error": {"message": resp.text}}
                return f"Error: HTTP {resp.status_code}: {err}", None
        except requests.exceptions.Timeout:
            return "Error: Request timed out", None
        except Exception as e:
            return f"Error: {str(e)}", None


class ChatThread(QThread):
    """Thread for handling chat requests to avoid blocking UI"""

    response_ready = Signal(str, object)  # response text, context
    error_occurred = Signal(str)

    def __init__(self, client, model, prompt, context=None, timeout=None):
        super().__init__()
        self.client = client
        self.model = model
        self.prompt = prompt
        self.context = context
        self.timeout = timeout

    def run(self):
        """Run the chat request"""
        try:
            response, context = self.client.generate_response(
                self.model, self.prompt, self.context, timeout=self.timeout
            )
            self.response_ready.emit(response, context)
        except Exception as e:
            self.error_occurred.emit(str(e))


class ChatWidget(QWidget):
    """Widget for chatting with AI about test results"""

    # Signal for connection check result
    connection_checked = Signal(bool)
    models_retrieved = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)

        # Initialize clients
        self.ollama_client = OllamaClient()
        self.openai_client = OpenAIClient()

        # Chat state
        self.conversation_context = None
        self.current_logs_dir = None
        self.current_result_dir = None
        self.current_log_content = ""
        self.chat_thread = None
        self._desired_ollama_model = None
        self._desired_openai_model = None
        # Settings store
        try:
            self.settings = QSettings("cps-test-lab", "ResultAnalyzerChat")
        except Exception:
            self.settings = None

        self.setup_ui()
        # Load persisted settings after UI exists
        self.load_settings()

        # Connect signals
        self.connection_checked.connect(self.update_connection_status)
        self.models_retrieved.connect(self.update_models_list)

        # Use a timer to check connection after UI is set up
        QTimer.singleShot(100, self.post_ui_init_checks)

        # Persist changes on user actions
        try:
            self.provider_combo.currentTextChanged.connect(self.save_settings)
            self.model_combo.currentTextChanged.connect(self.save_settings)
            self.openai_model_combo.currentTextChanged.connect(self.save_settings)
            self.timeout_spin.valueChanged.connect(self.save_settings)
            self.openai_timeout_spin.valueChanged.connect(self.save_settings)
            self.include_logs_cb.toggled.connect(self.save_settings)
            self.include_scenario_cb.toggled.connect(self.save_settings)
            self.filter_logs_cb.toggled.connect(self.save_settings)
            self.enable_gpt5_cb.toggled.connect(self.save_settings)
            self.openai_api_key_input.textChanged.connect(self.save_settings)
        except Exception:
            pass

    def post_ui_init_checks(self):
        """Run initial checks depending on selected provider."""
        provider = self.provider_combo.currentText()
        if provider == "Ollama":
            self.check_ollama_connection()
        else:
            # For OpenAI: if API key present, try loading models
            if self.openai_api_key_input.text().strip():
                self.refresh_models()

    def setup_ui(self):
        """Setup the chat widget UI"""
        layout = QVBoxLayout(self)

        # Connection status and model selection
        self.setup_header(layout)

        # Main chat area
        self.setup_chat_area(layout)

        # Data inclusion options
        self.setup_data_options(layout)

        # Input area
        self.setup_input_area(layout)

    def setup_header(self, parent_layout):
        """Setup header with provider selection, connection status, and model selection"""
        header_group = QGroupBox("AI Chat")
        header_layout = QVBoxLayout(header_group)

        # Provider selection row
        provider_layout = QHBoxLayout()
        provider_layout.addWidget(QLabel("Provider:"))
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(["Ollama", "OpenAI"])
        self.provider_combo.currentTextChanged.connect(self.on_provider_changed)
        provider_layout.addWidget(self.provider_combo)
        provider_layout.addStretch()
        self.clear_chat_btn = QPushButton("🗑 Clear Chat")
        self.clear_chat_btn.clicked.connect(self.clear_chat)
        provider_layout.addWidget(self.clear_chat_btn)
        header_layout.addLayout(provider_layout)

        # Container for Ollama-specific controls
        self.ollama_controls = QGroupBox("Ollama")
        ollama_v = QVBoxLayout(self.ollama_controls)
        status_layout = QHBoxLayout()
        self.connection_status = QLabel("Checking connection...")
        self.connection_status.setStyleSheet("color: orange;")
        status_layout.addWidget(self.connection_status)
        status_layout.addStretch()
        self.retry_connection_btn = QPushButton("🔄 Retry Connection")
        self.retry_connection_btn.clicked.connect(self.check_ollama_connection)
        status_layout.addWidget(self.retry_connection_btn)
        self.refresh_models_btn = QPushButton("🔄 Refresh Models")
        self.refresh_models_btn.clicked.connect(self.refresh_models)
        status_layout.addWidget(self.refresh_models_btn)
        ollama_v.addLayout(status_layout)

        ollama_model_layout = QHBoxLayout()
        ollama_model_layout.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(200)
        ollama_model_layout.addWidget(self.model_combo)
        timeout_label = QLabel("Timeout (s):")
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(1, 600)
        self.timeout_spin.setValue(120)
        self.timeout_spin.setToolTip("Request timeout in seconds")
        ollama_model_layout.addWidget(timeout_label)
        ollama_model_layout.addWidget(self.timeout_spin)
        ollama_model_layout.addStretch()
        ollama_v.addLayout(ollama_model_layout)

        # Container for OpenAI-specific controls
        self.openai_controls = QGroupBox("OpenAI")
        openai_v = QVBoxLayout(self.openai_controls)

        openai_row1 = QHBoxLayout()
        openai_row1.addWidget(QLabel("API Key:"))
        self.openai_api_key_input = QLineEdit()
        self.openai_api_key_input.setEchoMode(QLineEdit.Password)
        # Prefill from env if exists
        if self.openai_client.api_key:
            self.openai_api_key_input.setText(self.openai_client.api_key)
        self.openai_api_key_input.setPlaceholderText("sk-... or env OPENAI_API_KEY")
        self.openai_api_key_input.textChanged.connect(self.on_openai_api_key_changed)
        openai_row1.addWidget(self.openai_api_key_input)
        self.openai_refresh_btn = QPushButton("🔄 Load Models")
        self.openai_refresh_btn.clicked.connect(self.refresh_models)
        openai_row1.addWidget(self.openai_refresh_btn)
        openai_v.addLayout(openai_row1)

        openai_row2 = QHBoxLayout()
        openai_row2.addWidget(QLabel("Model:"))
        # Reuse the same model_combo for simplicity across providers
        # We'll show/hide groups but the combo widget itself is shared above in Ollama layout.
        # To keep UI consistent, create a separate combo for OpenAI and swap on provider change.
        self.openai_model_combo = QComboBox()
        self.openai_model_combo.setMinimumWidth(200)
        openai_row2.addWidget(self.openai_model_combo)

        self.enable_gpt5_cb = QCheckBox("Enable GPT-5 (Preview)")
        self.enable_gpt5_cb.setToolTip("Show gpt-5 preview model in the list if not returned by API")
        self.enable_gpt5_cb.toggled.connect(self.on_enable_gpt5_toggled)
        openai_row2.addWidget(self.enable_gpt5_cb)

        openai_row2.addStretch()
        # Share the same timeout control for both providers
        openai_row2.addWidget(QLabel("Timeout (s):"))
        self.openai_timeout_spin = QSpinBox()
        self.openai_timeout_spin.setRange(1, 600)
        self.openai_timeout_spin.setValue(120)
        self.openai_timeout_spin.setToolTip("Request timeout in seconds")
        openai_row2.addWidget(self.openai_timeout_spin)
        openai_v.addLayout(openai_row2)

        # Add groups to header
        header_layout.addWidget(self.ollama_controls)
        header_layout.addWidget(self.openai_controls)

        parent_layout.addWidget(header_group)

        # Initialize visibility
        self.on_provider_changed(self.provider_combo.currentText())

    def on_provider_changed(self, provider: str):
        is_ollama = provider == "Ollama"
        self.ollama_controls.setVisible(is_ollama)
        self.openai_controls.setVisible(not is_ollama)
        if is_ollama:
            self.check_ollama_connection()
        else:
            # Update status hint in chat area
            if not (self.openai_client.api_key or self.openai_api_key_input.text().strip()):
                self.add_message_to_chat("System", "OpenAI selected. Enter API key to load models.")
            # Auto-refresh models if key present
            if self.openai_api_key_input.text().strip():
                self.refresh_models()

    def on_openai_api_key_changed(self, text: str):
        self.openai_client.set_api_key(text.strip())
        # Optionally refresh models when key becomes non-empty
        if text.strip():
            self.refresh_models()

    def on_enable_gpt5_toggled(self, enabled: bool):
        label = "gpt-5 (Preview)"
        # Affect both combos so the option appears regardless of current provider
        if enabled:
            if getattr(self, 'openai_model_combo', None) is not None and self.openai_model_combo.findText(label) < 0:
                self.openai_model_combo.insertItem(0, label)
            if getattr(self, 'model_combo', None) is not None and self.model_combo.findText(label) < 0:
                self.model_combo.insertItem(0, label)
        else:
            if getattr(self, 'openai_model_combo', None) is not None:
                idx = self.openai_model_combo.findText(label)
                if idx >= 0:
                    self.openai_model_combo.removeItem(idx)
            if getattr(self, 'model_combo', None) is not None:
                idx2 = self.model_combo.findText(label)
                if idx2 >= 0:
                    self.model_combo.removeItem(idx2)

    def setup_chat_area(self, parent_layout):
        """Setup the main chat display area"""
        self.chat_display = QTextEdit()
        self.chat_display.setReadOnly(True)
        self.chat_display.setMinimumHeight(300)

        # Set font
        font = QFont("Consolas", 10)
        if not font.exactMatch():
            font = QFont("Courier New", 10)
        self.chat_display.setFont(font)

        # Apply theme-aware styling to the chat area
        self.apply_chat_styling()

        # Get theme-aware colors for welcome message
        welcome_msg = self.get_themed_welcome_message()
        self.chat_display.setHtml(welcome_msg)

        parent_layout.addWidget(self.chat_display)

    def apply_chat_styling(self):
        """Apply theme-aware styling to the chat display"""
        colors = self.get_theme_colors()

        # Set the background and text colors for the chat area
        style = f"""
        QTextEdit {{
            background-color: {colors["background"]};
            color: {colors["text"]};
            border: 1px solid {colors["border"]};
            border-radius: 5px;
            padding: 5px;
        }}
        """
        self.chat_display.setStyleSheet(style)

    def setup_data_options(self, parent_layout):
        """Setup data inclusion options"""
        options_group = QGroupBox("Include Data in Chat")
        options_layout = QHBoxLayout(options_group)

        self.include_logs_cb = QCheckBox("Include execution log (without shutdown)")
        self.include_logs_cb.setToolTip("Include the scenario execution log file in chat context")
        self.include_logs_cb.setChecked(True)
        options_layout.addWidget(self.include_logs_cb)

        self.include_scenario_cb = QCheckBox("Include scenario")
        self.include_scenario_cb.setToolTip("Include scenario files (.osc, .py, etc.) in chat context")
        self.include_scenario_cb.setChecked(True)
        options_layout.addWidget(self.include_scenario_cb)

        # Add filter option
        self.filter_logs_cb = QCheckBox("Filter logs")
        self.filter_logs_cb.setToolTip("Filter out rviz2 and sys_stats_publisher lines from logs")
        self.filter_logs_cb.setChecked(False)
        options_layout.addWidget(self.filter_logs_cb)

        # Future: Add more options here
        # self.include_test_results_cb = QCheckBox("Include test results")

        options_layout.addStretch()

        parent_layout.addWidget(options_group)

    def setup_input_area(self, parent_layout):
        """Setup input area for typing messages"""
        input_layout = QHBoxLayout()

        self.message_input = QLineEdit()
        self.message_input.setPlaceholderText("Type your message here... (Press Enter to send)")
        self.message_input.returnPressed.connect(self.send_message)
        input_layout.addWidget(self.message_input)

        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send_message)
        input_layout.addWidget(self.send_btn)

        # Progress bar for loading
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 0)  # Indeterminate progress

        parent_layout.addLayout(input_layout)
        parent_layout.addWidget(self.progress_bar)

    def is_dark_theme(self):
        """Detect if we're using a dark theme"""
        palette = self.palette()
        bg_color = palette.color(palette.Window)
        return bg_color.lightness() < 128

    def get_theme_colors(self):
        """Get theme-appropriate colors"""
        is_dark = self.is_dark_theme()

        if is_dark:
            return {
                'text': '#ffffff',
                'muted_text': '#cccccc',
                'background': '#2b2b2b',
                'card_background': '#3c3c3c',
                'border': '#555555',
                'user_color': '#66BB6A',      # Lighter green for better contrast
                'ai_color': '#42A5F5',        # Lighter blue for better contrast
                'error_color': '#EF5350',     # Lighter red for better contrast
                'welcome_text': '#bbbbbb'
            }
        else:
            return {
                'text': '#333333',
                'muted_text': '#666666',
                'background': '#ffffff',
                'card_background': '#f8f9fa',
                'border': '#e0e0e0',
                'user_color': '#2e7d32',      # Dark green for good contrast
                'ai_color': '#1976d2',        # Dark blue for good contrast
                'error_color': '#d32f2f',     # Dark red for good contrast
                'welcome_text': '#666666'
            }

    def get_themed_welcome_message(self):
        """Get theme-aware welcome message"""
        colors = self.get_theme_colors()
        return f"""
        <div style='color: {colors["welcome_text"]}; font-style: italic; margin-bottom: 10px;'>
        Welcome to AI Chat! This assistant can help you analyze test results and logs.<br>
        You can include log data in your questions using the options below.
        </div>
        """

    def refresh_theme(self):
        """Refresh the theme styling - call this when theme changes"""
        # Re-apply chat styling
        self.apply_chat_styling()

        # Clear and refresh the chat with new theme
        self.clear_chat()

    def check_ollama_connection(self):
        """Check Ollama connection and update UI"""
        def check_in_thread():
            try:
                is_connected = self.ollama_client.check_connection()
                # Use signal to safely update UI from thread
                self.connection_checked.emit(is_connected)
            except Exception as e:
                print(f"Error checking Ollama connection: {e}")
                # If there's an error, assume not connected
                self.connection_checked.emit(False)

        try:
            threading.Thread(target=check_in_thread, daemon=True).start()
        except Exception as e:
            print(f"Error starting connection check thread: {e}")
            # Fallback: check connection in main thread
            try:
                is_connected = self.ollama_client.check_connection()
                self.update_connection_status(is_connected)
            except Exception as e2:
                print(f"Error in fallback connection check: {e2}")
                self.update_connection_status(False)

    def update_connection_status(self, is_connected):
        """Update connection status in UI"""
        if not REQUESTS_AVAILABLE:
            self.connection_status.setText("❌ Python requests module not available")
            self.connection_status.setStyleSheet("color: red;")
            self.model_combo.clear()
            self.model_combo.addItem("Requests module required")
            return

        # Only applies to Ollama; OpenAI has separate cues
        if self.provider_combo.currentText() == "Ollama":
            if is_connected:
                self.connection_status.setText("✅ Connected to Ollama")
                self.connection_status.setStyleSheet("color: green;")
                self.refresh_models()
            else:
                self.connection_status.setText("❌ Ollama not available (localhost:11434)")
                self.connection_status.setStyleSheet("color: red;")
                self.model_combo.clear()
                self.model_combo.addItem("No models available")

    def refresh_models(self):
        """Refresh available models for the selected provider"""
        provider = self.provider_combo.currentText()

        def get_models_in_thread():
            try:
                if provider == "Ollama":
                    models = self.ollama_client.list_models()
                else:
                    models = self.openai_client.list_models()
                    # Optionally add GPT-5 preview
                    if self.enable_gpt5_cb.isChecked():
                        preview_label = "gpt-5 (Preview)"
                        if "gpt-5" not in models and preview_label not in models:
                            models = [preview_label] + models
                # Use signal to safely update UI from thread
                self.models_retrieved.emit(models)
            except Exception as e:
                print(f"Error getting models for {provider}: {e}")
                # If there's an error, show empty list
                self.models_retrieved.emit([])

        try:
            threading.Thread(target=get_models_in_thread, daemon=True).start()
        except Exception as e:
            print(f"Error starting models refresh thread: {e}")
            # Fallback: update with empty list
            self.update_models_list([])

    def update_models_list(self, models):
        """Update models list in UI depending on provider"""
        provider = self.provider_combo.currentText()
        if provider == "Ollama":
            self.model_combo.clear()
            if models:
                self.model_combo.addItems(models)
                # Prefer saved model if available
                if self._desired_ollama_model:
                    idx_saved = self.model_combo.findText(self._desired_ollama_model)
                    if idx_saved >= 0:
                        self.model_combo.setCurrentIndex(idx_saved)
                        return
                # Try to select a sensible default
                for preferred in ['llama3.2', 'llama3.1', 'llama3', 'mistral', 'codellama']:
                    for model in models:
                        if preferred in str(model).lower():
                            index = self.model_combo.findText(str(model))
                            if index >= 0:
                                self.model_combo.setCurrentIndex(index)
                                return
            else:
                self.model_combo.addItem("No models available")
        else:
            self.openai_model_combo.clear()
            if models:
                # Normalize if we included preview label
                display_models = [str(m) for m in models]
                self.openai_model_combo.addItems(display_models)
                # Prefer saved model if available
                if self._desired_openai_model:
                    idx_saved = self.openai_model_combo.findText(self._desired_openai_model)
                    if idx_saved >= 0:
                        self.openai_model_combo.setCurrentIndex(idx_saved)
                        return
                # Prefer GPT-5 preview or popular models
                preferred_openai = ['gpt-5 (Preview)', 'gpt-5', 'gpt-5-preview', 'gpt-4o', 'gpt-4o-mini', 'gpt-4.1', 'gpt-4.1-mini']
                for preferred in preferred_openai:
                    idx = self.openai_model_combo.findText(preferred)
                    if idx >= 0:
                        self.openai_model_combo.setCurrentIndex(idx)
                        return
            else:
                self.openai_model_combo.addItem("No models available")

    def save_settings(self):
        """Persist chat widget settings."""
        if not hasattr(self, 'settings') or self.settings is None:
            return
        try:
            self.settings.setValue("provider", self.provider_combo.currentText())
            self.settings.setValue("include_logs", self.include_logs_cb.isChecked())
            self.settings.setValue("include_scenario", self.include_scenario_cb.isChecked())
            self.settings.setValue("filter_logs", self.filter_logs_cb.isChecked())
            self.settings.setValue("enable_gpt5_preview", self.enable_gpt5_cb.isChecked())
            self.settings.setValue("ollama_timeout", self.timeout_spin.value())
            self.settings.setValue("openai_timeout", self.openai_timeout_spin.value())
            self.settings.setValue("ollama_model", self.model_combo.currentText())
            self.settings.setValue("openai_model", self.openai_model_combo.currentText())
            # Store API key in user settings (local to this user)
            self.settings.setValue("openai_api_key", self.openai_api_key_input.text())
            self.settings.sync()
        except Exception as e:
            print(f"Error saving settings: {e}")

    def load_settings(self):
        """Load chat widget settings and apply to UI."""
        if not hasattr(self, 'settings') or self.settings is None:
            return
        try:
            provider = self.settings.value("provider")
            if provider in ("Ollama", "OpenAI"):
                idx = self.provider_combo.findText(provider)
                if idx >= 0:
                    self.provider_combo.setCurrentIndex(idx)

            def read_bool(key, default):
                val = self.settings.value(key)
                if val is None:
                    return default
                if isinstance(val, bool):
                    return val
                s = str(val).strip().lower()
                return s in ("1", "true", "yes", "on")

            self.include_logs_cb.setChecked(read_bool("include_logs", True))
            self.include_scenario_cb.setChecked(read_bool("include_scenario", True))
            self.filter_logs_cb.setChecked(read_bool("filter_logs", False))
            self.enable_gpt5_cb.setChecked(read_bool("enable_gpt5_preview", False))

            try:
                self.timeout_spin.setValue(int(self.settings.value("ollama_timeout", 120)))
            except Exception:
                pass
            try:
                self.openai_timeout_spin.setValue(int(self.settings.value("openai_timeout", 120)))
            except Exception:
                pass

            saved_key = self.settings.value("openai_api_key")
            if saved_key:
                self.openai_api_key_input.setText(saved_key)
                self.openai_client.set_api_key(str(saved_key))

            self._desired_ollama_model = self.settings.value("ollama_model") or None
            self._desired_openai_model = self.settings.value("openai_model") or None
        except Exception as e:
            print(f"Error loading settings: {e}")

    def set_logs_directory(self, logs_dir):
        """Set the current logs directory and clear chat context when result changes"""
        # Check if this is a different logs directory (new result selected)
        if self.current_logs_dir != logs_dir:
            # Only show the system message if there was a previous conversation context
            had_context = self.conversation_context is not None

            # Clear the chat context when switching to a new result
            self.conversation_context = None

            self.clear_chat()

        self.current_logs_dir = logs_dir
        # Set result directory (parent of logs directory)
        if logs_dir:
            self.current_result_dir = Path(logs_dir).parent
        else:
            self.current_result_dir = None

    def get_scenario_files(self):
        """Get scenario files from the current result directory"""
        scenario_files = []

        if not self.current_result_dir:
            return scenario_files

        # Common scenario file extensions
        scenario_extensions = ['.osc', '.variant']

        try:
            # Look for scenario files in the result directory and subdirectories
            for ext in scenario_extensions:
                # Search in result directory
                scenario_files.extend(self.current_result_dir.glob(f'*{ext}'))

        except Exception as e:
            print(f"Error finding scenario files: {e}")

        return scenario_files

    def get_context_data(self):
        """Get context data to include in chat"""
        context_parts = []

        if self.include_logs_cb.isChecked():
            # Get current log content from log viewer
            log_file = get_scenario_execution_log_file(self.current_logs_dir)
            if log_file:
                try:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        log_content = f.read()
                    print(f"log content length: {len(log_content)}")
                    # Truncate log content after ": execution failed."
                    truncate_marker = ": execution failed."
                    truncate_index = log_content.find(truncate_marker)
                    if truncate_index != -1:
                        log_content = log_content[:truncate_index + len(truncate_marker)]
                    else:
                        # Truncate after "Scenario '<variable name>' succeeded."
                        match = re.search(r"Scenario '([^']+)' succeeded\.", log_content)
                        if match:
                            end_index = match.end()
                            log_content = log_content[:end_index]
                    print(f"log content length without shutdown: {len(log_content)}")

                    # Apply filtering if enabled
                    if self.filter_logs_cb.isChecked():
                        log_content = filter_nonrelevant_lines(log_content)
                        print(f"log content length without irrelevant nodes: {len(log_content)}")

                    if log_content:
                        context_parts.append(f"Execution Log Content:\n{log_content}")
                except Exception as e:
                    print(f"Error getting log content: {e}")

        if self.include_scenario_cb.isChecked():
            # Get scenario files content
            scenario_files = self.get_scenario_files()
            for scenario_file in scenario_files:
                try:
                    with open(scenario_file, "r", encoding="utf-8", errors="replace") as f:
                        scenario_content = f.read()
                    if scenario_content:
                        # Limit scenario file size to avoid overwhelming the context
                        max_scenario_size = 5000  # 5KB per file
                        if len(scenario_content) > max_scenario_size:
                            scenario_content = scenario_content[:max_scenario_size] + "\n... (content truncated)"
                        context_parts.append(f"Scenario File ({scenario_file.name}):\n{scenario_content}")
                        print(f"Added scenario file: {scenario_file.name} ({len(scenario_content)} chars)")
                except Exception as e:
                    print(f"Error reading scenario file {scenario_file}: {e}")

        return "\n\n".join(context_parts) if context_parts else None

    def send_message(self):
        """Send message to AI"""
        message = self.message_input.text().strip()
        if not message:
            return

        # Check provider and model
        provider = self.provider_combo.currentText()
        if provider == "Ollama":
            current_model = self.model_combo.currentText()
        else:
            current_model = self.openai_model_combo.currentText()

        if not current_model or current_model == "No models available":
            QMessageBox.warning(self, "No Model", "Please select a valid model.")
            return

        # Map preview label to actual model id
        if provider == "OpenAI" and current_model == "gpt-5 (Preview)":
            current_model = "gpt-5"

        # Clear input
        self.message_input.clear()

        # Add user message to chat
        self.add_message_to_chat("You", message, is_user=True)

        # Prepare context
        context_data = self.get_context_data()
        full_prompt = message

        if context_data:
            full_prompt = f"Context:\n{context_data}\n\nUser Question: {message}"

        # Show progress
        self.progress_bar.setVisible(True)
        self.send_btn.setEnabled(False)
        self.message_input.setEnabled(False)

        # Start chat thread
        if provider == "Ollama":
            client = self.ollama_client
            req_timeout = self.timeout_spin.value() if getattr(self, 'timeout_spin', None) else None
        else:
            client = self.openai_client
            req_timeout = self.openai_timeout_spin.value() if getattr(self, 'openai_timeout_spin', None) else None

        self.chat_thread = ChatThread(
            client,
            current_model,
            full_prompt,
            self.conversation_context,
            timeout=req_timeout
        )
        self.chat_thread.response_ready.connect(self.on_response_ready)
        self.chat_thread.error_occurred.connect(self.on_error_occurred)
        self.chat_thread.start()

    def on_response_ready(self, response, context):
        """Handle AI response"""
        self.conversation_context = context
        self.add_message_to_chat("AI", response, is_user=False)

        # Hide progress
        self.progress_bar.setVisible(False)
        self.send_btn.setEnabled(True)
        self.message_input.setEnabled(True)
        self.message_input.setFocus()

    def on_error_occurred(self, error_msg):
        """Handle error in chat"""
        self.add_message_to_chat("System", f"Error: {error_msg}", is_error=True)

        # Hide progress
        self.progress_bar.setVisible(False)
        self.send_btn.setEnabled(True)
        self.message_input.setEnabled(True)
        self.message_input.setFocus()

    def add_message_to_chat(self, sender, message, is_user=False, is_error=False):
        """Add message to chat display"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        colors = self.get_theme_colors()

        # Determine colors and formatting based on theme
        if is_error:
            color = colors['error_color']
            prefix = "⚠️"
        elif is_user:
            color = colors['user_color']
            prefix = "👤"
        elif sender == "System":
            color = colors['muted_text']
            prefix = "ℹ️"
        else:
            color = colors['ai_color']
            prefix = "🤖"

        # Escape HTML in the message and preserve line breaks
        import html
        escaped_message = html.escape(message)
        # Convert newlines to HTML line breaks
        escaped_message = escaped_message.replace('\n', '<br>')

        # Enhanced formatting for AI responses
        if not is_user and not is_error:
            escaped_message = self.format_ai_response(escaped_message)

        # Don't escape the emoji prefix - keep it as raw Unicode
        escaped_prefix = prefix

        # Format message with theme-aware colors
        formatted_message = f"""
        <div style='margin-bottom: 15px; padding: 10px; border-left: 3px solid {color}; background-color: {colors["card_background"]}; border-radius: 5px;'>
            <div style='color: {color}; font-weight: bold; margin-bottom: 5px;'>
                <br><br><span style='font-family: "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", sans-serif;'>{escaped_prefix}</span> {sender} <span style='color: {colors["muted_text"]}; font-size: 0.8em;'>{timestamp}</span>
            </div>
            <div style='color: {colors["text"]}; white-space: pre-wrap; word-wrap: break-word; line-height: 1.4;'>{escaped_message}</div>
        </div>
        """

        # Add to chat display
        cursor = self.chat_display.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertHtml(formatted_message)

        # Auto-scroll to bottom
        scrollbar = self.chat_display.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def format_ai_response(self, text):
        """Format AI response text to make it more visually appealing"""
        import re

        # Get theme colors for consistent styling
        colors = self.get_theme_colors()
        is_dark = self.is_dark_theme()

        # Define colors based on theme
        if is_dark:
            header_color = "#81C784"  # Light green for headers
            code_bg = "#2C2C2C"      # Darker background for code
            code_border = "#555555"   # Border for code blocks
            code_text = "#E8E8E8"    # Light text for code
            emphasis_color = "#FFB74D"  # Orange for emphasis
        else:
            header_color = "#2E7D32"  # Dark green for headers
            code_bg = "#F5F5F5"      # Light background for code
            code_border = "#CCCCCC"   # Border for code blocks
            code_text = "#333333"    # Dark text for code
            emphasis_color = "#F57C00"  # Orange for emphasis

        # Format headers (lines starting with **text**)
        text = re.sub(
            r'\*\*(.*?)\*\*',
            f'<span style="color: {header_color}; font-weight: bold; font-size: 1.1em;">\\1</span>',
            text
        )

        # Format code blocks (```text```)
        def format_code_block(match):
            code_content = match.group(1)
            return f'''<div style="
                background-color: {code_bg};
                border: 1px solid {code_border};
                border-radius: 4px;
                padding: 8px;
                margin: 8px 0;
                font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
                font-size: 0.9em;
                color: {code_text};
                overflow-x: auto;
                white-space: pre;
            ">{code_content}</div>'''

        text = re.sub(r'```([^`]*?)```', format_code_block, text, flags=re.DOTALL)

        # Format inline code (`text`)
        text = re.sub(
            r'`([^`]+)`',
            f'<code style="background-color: {code_bg}; color: {
                code_text}; padding: 2px 4px; border-radius: 3px; font-family: monospace; font-size: 0.9em;">\\1</code>',
            text
        )

        # Format emphasis/italic (*text*)
        text = re.sub(
            r'\*([^*]+)\*',
            f'<em style="color: {emphasis_color}; font-style: italic;">\\1</em>',
            text
        )

        # Format lists (lines starting with - or *)
        text = re.sub(
            r'<br>[-*]\s+(.+)',
            f'<br>• <span style="margin-left: 10px;">\\1</span>',
            text
        )

        # Format numbered lists (lines starting with numbers)
        text = re.sub(
            r'<br>(\d+)\.\s+(.+)',
            f'<br><span style="color: {header_color}; font-weight: bold;">\\1.</span> <span style="margin-left: 5px;">\\2</span>',
            text
        )

        return text

    def clear_chat(self):
        """Clear chat history"""
        self.chat_display.clear()
        self.conversation_context = None

        # Re-add welcome message with theme-aware colors
        welcome_msg = self.get_themed_welcome_message()
        self.chat_display.setHtml(welcome_msg)

    def clear(self):
        """Clear the widget"""
        self.clear_chat()
        self.current_logs_dir = None
        self.current_result_dir = None
        self.current_log_content = ""
