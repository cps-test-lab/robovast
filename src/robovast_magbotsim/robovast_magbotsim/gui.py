# Copyright (C) 2025 RoboVAST Contributors
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

try:
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
    from matplotlib.figure import Figure
    from matplotlib.colors import ListedColormap
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    import numpy as np
    _PYSIDE6_AVAILABLE = True
except ImportError:
    _PYSIDE6_AVAILABLE = False
    QWidget = object  # type: ignore[assignment,misc]


if _PYSIDE6_AVAILABLE:

    class MagBotSimTileLayoutWidget(QWidget):
        """Qt Widget for visualizing MagBotSim tile layouts."""

        def __init__(self, parent=None):
            """Initialize the tile layout visualization widget.
            
            Args:
                parent: Parent QWidget (optional)
            """
            super().__init__(parent)
            self.figure = Figure(figsize=(8, 8))
            self.canvas = FigureCanvas(self.figure)
            self.toolbar = NavigationToolbar(self.canvas, self)
            
            # Set up the layout
            layout = QVBoxLayout()
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self.toolbar)
            layout.addWidget(self.canvas)
            self.setLayout(layout)
            
            # Apply dark theme
            self.figure.patch.set_facecolor('#2b2b2b')
            self.toolbar.setStyleSheet("""
                QToolBar {
                    background-color: #2b2b2b;
                    border: none;
                    spacing: 3px;
                }
                QToolButton {
                    background-color: #3c3c3c;
                    border: 1px solid #555555;
                    border-radius: 3px;
                    padding: 5px;
                    color: #ffffff;
                }
                QToolButton:hover {
                    background-color: #4a4a4a;
                    border: 1px solid #6a6a6a;
                }
            """)

        def draw_layout(self, layout_tiles):
            """Draw the tile layout as a grid.
            
            Args:
                layout_tiles: 2D numpy array where 1 = tile, 0 = empty
            """
            self.figure.clear()
            ax = self.figure.add_subplot(111)
            
            # Create custom colormap: empty space = dark gray, tile = light blue
            colors = ['#3c3c3c', '#4da6ff']
            cmap = ListedColormap(colors)
            
            # Display the layout
            im = ax.imshow(layout_tiles.T, cmap=cmap, origin='lower', aspect='auto')
            
            # Set up grid and labels
            ax.set_xlabel('X (tiles)', color='#ffffff')
            ax.set_ylabel('Y (tiles)', color='#ffffff')
            ax.set_title('MagBotSim Tile Layout', color='#ffffff', fontsize=12, fontweight='bold')
            
            # Style the axes
            ax.tick_params(colors='#ffffff')
            ax.spines['bottom'].set_color('#ffffff')
            ax.spines['left'].set_color('#ffffff')
            ax.spines['top'].set_color('#2b2b2b')
            ax.spines['right'].set_color('#2b2b2b')
            
            # Add grid
            ax.set_xticks(np.arange(-0.5, layout_tiles.shape[0], 1), minor=True)
            ax.set_yticks(np.arange(-0.5, layout_tiles.shape[1], 1), minor=True)
            ax.grid(which='minor', color='#555555', linestyle='-', linewidth=0.5)
            
            # Add tile count info
            num_tiles = np.sum(layout_tiles)
            total_grid_size = layout_tiles.shape[0] * layout_tiles.shape[1]
            coverage = (num_tiles / total_grid_size * 100) if total_grid_size > 0 else 0
            
            info_text = f'Tiles: {int(num_tiles)} / Grid: {layout_tiles.shape[0]}×{layout_tiles.shape[1]} ({coverage:.1f}%)'
            ax.text(0.5, -0.12, info_text, transform=ax.transAxes, 
                    ha='center', color='#ffffff', fontsize=10)
            
            self.figure.tight_layout()
            self.canvas.draw()


    class MagBotSimEnvGui(QWidget):
        """GUI for MagBotSimEnv Variations."""

        def __init__(self, parent=None):
            """Initialize the GUI.
            
            Args:
                parent: Parent QWidget (optional)
            """
            super().__init__(parent)
            self.tile_widget = MagBotSimTileLayoutWidget(self)
            
            layout = QVBoxLayout()
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self.tile_widget)
            self.setLayout(layout)

        def update(self, config, path):
            """Update the GUI with the given config data.
            
            Args:
                config: The config data to display.
                path: The file path of the config.
            """
            layout_tiles = config.get('config', {}).get('layout_tiles', None)
            if layout_tiles is not None:
                # Convert to numpy array if it's a list
                if isinstance(layout_tiles, list):
                    layout_tiles = np.array(layout_tiles, dtype=np.int8)
                self.tile_widget.draw_layout(layout_tiles)

else:
    # Placeholder classes for headless environments
    MagBotSimTileLayoutWidget = None  # type: ignore[assignment]
    MagBotSimEnvGui = None  # type: ignore[assignment]

