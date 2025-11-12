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

import yaml
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QLabel, QSplitter, QTabWidget, QVBoxLayout,
                               QWidget)

from robovast.common import convert_dataclasses_to_dict, filter_variants
from robovast.configuration.gui.yaml_editor import YamlEditor


class VariantView(QWidget):

    """View for displaying variant information."""

    def __init__(self, parent=None, debug=False):
        super().__init__(parent)
        self.gui_classes = []
        self.gui_instances = []
        self.plugin_widget_renderer = []
        self.plugin_widgets = []
        self.debug = debug
        self.init_ui()

    def init_ui(self):
        """Initialize the user interface."""
        layout = QVBoxLayout()
        self.setLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)

        self.label = QLabel("Variant Details")
        layout.addWidget(self.label)

        # Use YAML editor for displaying variant data
        self.info_display = YamlEditor()
        self.info_display.setReadOnly(True)

        layout.addWidget(self.info_display)

        # Placeholder for splitter (will be created when needed)
        self.splitter = None
        self.gui_container = None

    def clear(self):
        self.update_variants({}, None)
        self.info_display.clear()

    def update_variants(self, gui_classes, variant_file_path):
        """Set GUI class for variant parameters.

        Args:
            gui_classes: The GUI classes to set.
        """
        self.gui_classes = gui_classes
        self.variant_file_path = variant_file_path
        self._rebuild_layout()

    def _rebuild_layout(self):
        """Rebuild the layout based on whether gui_classes is empty or not."""
        layout = self.layout()

        # Clean up old plugin widgets and renderers
        self.plugin_widgets.clear()
        self.plugin_widget_renderer.clear()

        # Remove splitter if it exists
        if self.splitter:
            layout.removeWidget(self.splitter)
            self.splitter.deleteLater()
            self.splitter = None
            self.gui_container = None

        # If no gui_classes, just use the simple layout (info_display already added)
        if not self.gui_classes:
            if self.info_display.parent() != self:
                layout.addWidget(self.info_display)
            return

        # If gui_classes exist, create a splitter
        if self.info_display.parent() == self:
            layout.removeWidget(self.info_display)

        self.splitter = QSplitter(Qt.Vertical)
        layout.addWidget(self.splitter)

        # Add info_display to top of splitter
        self.splitter.addWidget(self.info_display)

        # Create GUI container based on number of gui_classes
        if len(self.gui_classes) == 1:

            # Single GUI class - show directly without tabs
            first_gui_class = next(iter(self.gui_classes))
            widget_instance = first_gui_class(None)
            for renderer in self.gui_classes[first_gui_class]:
                renderer_instance = renderer(widget_instance)
                self.plugin_widget_renderer.append(renderer_instance)
            self.gui_container = None
            self.plugin_widgets = [widget_instance]
            self.splitter.addWidget(widget_instance)
        else:
            # Multiple GUI classes - use tab widget
            self.gui_container = QTabWidget()
            for gui_class in self.gui_classes:
                gui_instance = gui_class(None)
                self.plugin_widgets.append(gui_instance)
                tab_label = gui_class.__name__
                self.gui_container.addTab(gui_instance, tab_label)
                for renderer in self.gui_classes[gui_class]:
                    renderer_instance = renderer(gui_instance)
                    self.plugin_widget_renderer.append(renderer_instance)
            self.splitter.addWidget(self.gui_container)

    def update_variant_info(self, variant_data):
        """Update the displayed variant information.

        Args:
            variant_data: The variant information to display.
        """
        if self.debug:
            # In debug mode, skip filtering to show all internal values
            converted_variant = convert_dataclasses_to_dict(variant_data)
        else:
            # In normal mode, filter out internal values starting with _
            filtered_variants = filter_variants([variant_data])
            converted_variant = convert_dataclasses_to_dict(filtered_variants[0])

        self.info_display.setPlainText(yaml.dump(converted_variant))

        for widget in self.plugin_widgets:
            widget.update(variant_data, self.variant_file_path)
        for renderer in self.plugin_widget_renderer:
            renderer.update_gui(variant_data, self.variant_file_path)
