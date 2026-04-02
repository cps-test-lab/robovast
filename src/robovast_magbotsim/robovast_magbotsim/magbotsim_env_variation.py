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

import logging
import random
from collections import deque

import numpy as np
from pydantic import Field

from robovast.common.config import VariationConfig
from robovast.common.variation import VariationGuiRenderer
from robovast.common.variation.base_variation import Variation

logger = logging.getLogger(__name__)

# Lazy import for GUI classes to avoid loading PySide6 in headless environments
def _get_gui_classes():
    """Lazy load GUI classes only when needed (GUI context)."""
    try:
        from .gui import MagBotSimEnvGui, MagBotSimTileLayoutWidget
        return MagBotSimEnvGui, MagBotSimTileLayoutWidget
    except ImportError:
        return None, None


class MagBotSimEnvVariationConfig(VariationConfig):
    """Configuration for MagBotSimEnvVariation.
    
    Generates valid tile layouts (layout_tiles) for BasicMagBotEnv environments.
    All tiles in generated layouts are guaranteed to be reachable from each other
    using only 4-connectivity (up, down, left, right).
    """
    
    num_variations: int = Field(default=1, description="Number of layout variations to generate")
    grid_width: int = Field(default=5, description="Width of the tile grid (x dimension)")
    grid_height: int = Field(default=5, description="Height of the tile grid (y dimension)")
    num_tiles: int | None = Field(default=None, description="Target number of tiles in layout (None = random)")
    seed: int = Field(default=42, description="Random seed for reproducibility")
    strategy: str = Field(default="random_walk", description="Layout generation strategy: 'random_walk' or 'sparse'")


class MagBotSimEnvVariationGuiRenderer(VariationGuiRenderer):
    """Renderer for visualizing MagBotSim tile layouts in the config GUI."""
    
    def update_gui(self, config, path):
        """Update the GUI with tile layout visualization.
        
        Args:
            config: The config data containing layout_tiles
            path: The file path of the config
        """
        layout_tiles = config.get('config', {}).get('layout_tiles', None)
        if layout_tiles is not None and hasattr(self.gui_object, 'tile_widget'):
            # Convert to numpy array if it's a list
            if isinstance(layout_tiles, list):
                layout_tiles = np.array(layout_tiles, dtype=np.int8)
            # Draw the layout
            self.gui_object.tile_widget.draw_layout(layout_tiles)


class MagBotSimEnvVariation(Variation):
    """Generates tile layout variations for MagBotSimEnv.
    
    Creates 2D numpy arrays representing tile layouts where:
    - 1 indicates a tile is present
    - 0 indicates a missing tile
    
    All generated layouts guarantee connectivity: every tile is reachable 
    from every other tile using only orthogonal (4-connected) movement.
    
    Expected parameters:
    - num_variations: Number of layout variations to generate
    - grid_width: Width of the grid (x dimension)
    - grid_height: Height of the grid (y dimension)
    - num_tiles: Target number of tiles (None = random)
    - seed: Random seed for reproducibility
    - strategy: Layout generation strategy ('random_walk' or 'sparse')
    
    Example YAML:
    
    .. code-block:: yaml
    
        - MagBotSimEnvVariation:
            num_variations: 5
            grid_width: 6
            grid_height: 6
            num_tiles: 20
            seed: 42
            strategy: random_walk
    """
    
    CONFIG_CLASS = MagBotSimEnvVariationConfig
    GUI_RENDERER_CLASS = MagBotSimEnvVariationGuiRenderer
    
    # GUI_CLASS will be set to MagBotSimEnvGui if PySide6 is available
    # (set at module level after class definition to support lazy loading)
    GUI_CLASS = None
    
    def variation(self, in_configs):
        """Generate tile layout variations.
        
        Args:
            in_configs: Input configurations to extend with layout_tiles
            
        Returns:
            List of configurations with generated layout_tiles
        """
        self.progress_update(
            f"Generating {self.parameters.num_variations} MagBotSimEnv layouts "
            f"({self.parameters.grid_width}x{self.parameters.grid_height})"
        )
        
        # Set random seed for reproducibility
        random.seed(self.parameters.seed)
        np.random.seed(self.parameters.seed)
        
        # If no input configs, create initial empty config
        if not in_configs or len(in_configs) == 0:
            in_configs = [{'config': {}, 'name': 'base'}]
        
        results = []
        
        # Generate layouts
        for i in range(self.parameters.num_variations):
            layout_tiles = self._generate_layout(
                self.parameters.grid_width,
                self.parameters.grid_height,
                self.parameters.num_tiles,
                self.parameters.strategy
            )
            
            # Create variations for each input config
            for config in in_configs:
                new_config = self.update_config(
                    config,
                    scenario_values={'layout_tiles': layout_tiles},
                    config_files=[]
                )
                results.append(new_config)
                
            self.progress_update(
                f"Generated layout {i+1}/{self.parameters.num_variations}: "
                f"{np.sum(layout_tiles)} tiles"
            )
        
        return results
    
    def _generate_layout(self, width: int, height: int, target_tiles: int | None = None, 
                        strategy: str = "random_walk") -> np.ndarray:
        """Generate a connected tile layout.
        
        Args:
            width: Grid width (x dimension)
            height: Grid height (y dimension)
            target_tiles: Target number of tiles (None = random)
            strategy: Generation strategy ('random_walk' or 'sparse')
            
        Returns:
            2D numpy array of shape (width, height) with 1s for tiles and 0s for empty
        """
        if strategy == "random_walk":
            return self._generate_random_walk_layout(width, height, target_tiles)
        elif strategy == "sparse":
            return self._generate_sparse_layout(width, height, target_tiles)
        else:
            raise ValueError(f"Unknown strategy: {strategy}")
    
    def _generate_random_walk_layout(self, width: int, height: int, 
                                    target_tiles: int | None = None) -> np.ndarray:
        """Generate layout using random walk algorithm.
        
        Starts from center and randomly walks, adding tiles.
        Guarantees connectivity as all tiles are added via walking.
        
        Args:
            width: Grid width
            height: Grid height
            target_tiles: Target number of tiles (None = 50-80% of grid)
            
        Returns:
            Connected layout as 2D numpy array
        """
        layout = np.zeros((width, height), dtype=np.int8)
        
        # Determine target number of tiles
        max_tiles = width * height
        if target_tiles is None:
            target_tiles = random.randint(int(0.5 * max_tiles), int(0.8 * max_tiles))
        else:
            target_tiles = min(target_tiles, max_tiles)
        
        # Start from random position
        x, y = random.randint(0, width - 1), random.randint(0, height - 1)
        layout[x, y] = 1
        visited = {(x, y)}
        
        # Random walk to add tiles
        while len(visited) < target_tiles:
            # Choose random direction (up, down, left, right)
            directions = [(0, 1), (0, -1), (1, 0), (-1, 0)]
            random.shuffle(directions)
            
            moved = False
            for dx, dy in directions:
                nx, ny = x + dx, y + dy
                
                # Check bounds
                if 0 <= nx < width and 0 <= ny < height:
                    layout[nx, ny] = 1
                    x, y = nx, ny
                    visited.add((x, y))
                    moved = True
                    break
            
            # If stuck, retry from random visited position
            if not moved and len(visited) < target_tiles:
                x, y = random.choice(list(visited))
        
        return layout
    
    def _generate_sparse_layout(self, width: int, height: int, 
                               target_tiles: int | None = None) -> np.ndarray:
        """Generate sparse layout with guaranteed connectivity.
        
        Uses a spanning tree approach: creates minimum spanning tree then adds
        random tiles while maintaining connectivity.
        
        Args:
            width: Grid width
            height: Grid height
            target_tiles: Target number of tiles (None = 40-70% of grid)
            
        Returns:
            Connected layout as 2D numpy array
        """
        layout = np.zeros((width, height), dtype=np.int8)
        max_tiles = width * height
        
        if target_tiles is None:
            target_tiles = random.randint(int(0.4 * max_tiles), int(0.7 * max_tiles))
        else:
            target_tiles = min(target_tiles, max_tiles)
        
        # Initialize visited set with random starting tile
        visited = set()
        frontier = []
        
        # Start from center or random position
        start_x, start_y = width // 2, height // 2
        layout[start_x, start_y] = 1
        visited.add((start_x, start_y))
        
        # Add neighbors of start position to frontier
        for dx, dy in [(0, 1), (0, -1), (1, 0), (-1, 0)]:
            nx, ny = start_x + dx, start_y + dy
            if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in visited:
                frontier.append((nx, ny))
        
        # Grow layout using randomized algorithm
        while len(visited) < target_tiles and frontier:
            # Pick random position from frontier
            idx = random.randint(0, len(frontier) - 1)
            x, y = frontier[idx]
            frontier[idx] = frontier[-1]
            frontier.pop()
            
            if (x, y) not in visited:
                layout[x, y] = 1
                visited.add((x, y))
                
                # Add unvisited neighbors to frontier
                for dx, dy in [(0, 1), (0, -1), (1, 0), (-1, 0)]:
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in visited:
                        if (nx, ny) not in frontier:
                            frontier.append((nx, ny))
        
        # Verify connectivity
        if not self._is_connected(layout):
            # Fallback to random walk if sparse method fails
            return self._generate_random_walk_layout(width, height, target_tiles)
        
        return layout
    
    def _is_connected(self, layout: np.ndarray) -> bool:
        """Check if all tiles in layout are connected using 4-connectivity.
        
        Args:
            layout: 2D array where 1 = tile, 0 = empty
            
        Returns:
            True if all tiles are connected, False otherwise
        """
        # Find first tile
        nonzero = np.nonzero(layout)
        if len(nonzero[0]) == 0:
            return True  # Empty layout is trivially connected
        
        start = (nonzero[0][0], nonzero[1][0])
        
        # BFS to find all reachable tiles
        visited = set()
        queue = deque([start])
        visited.add(start)
        
        while queue:
            x, y = queue.popleft()
            
            for dx, dy in [(0, 1), (0, -1), (1, 0), (-1, 0)]:
                nx, ny = x + dx, y + dy
                
                if (0 <= nx < layout.shape[0] and 
                    0 <= ny < layout.shape[1] and 
                    (nx, ny) not in visited and 
                    layout[nx, ny] == 1):
                    visited.add((nx, ny))
                    queue.append((nx, ny))
        
        # All tiles should be reachable
        num_tiles = np.sum(layout)
        return len(visited) == num_tiles


# Set GUI_CLASS if PySide6 is available (lazy loading for headless environments)
try:
    _gui_class, _ = _get_gui_classes()
    if _gui_class is not None:
        MagBotSimEnvVariation.GUI_CLASS = _gui_class
except Exception:
    # If GUI loading fails, just leave GUI_CLASS as None
    pass

