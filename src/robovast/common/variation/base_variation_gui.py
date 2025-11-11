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


from PySide6.QtWidgets import QWidget


class VariationGuiRenderer:

    def __init__(self, gui_object):
        """Initialize the VariationGuiRenderer.

        Args:
            gui_object: The GUI object to render the variation on.
        """
        self.gui_object = gui_object

    def update_gui(self, variant, path):
        """Update the GUI with the given variant data.

        Args:
            gui_object: The GUI object to update.
            variant: The variant data to display.
            path: The file path of the variant.
        """


class VariationGui(QWidget):

    def update_gui(self, variant, path):
        """Update the GUI with the given variant data.

        Args:
            gui_object: The GUI object to update.
            variant: The variant data to display.
            path: The file path of the variant.
        """
