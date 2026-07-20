// Importing this module registers every built-in run-view panel with the registry (each panel file
// calls registerPanel as a side effect). RunView imports it once before parsing the vast panels.
import './PlaybackPanel'
import './CostmapPanel'
import './ScenarioTreePanel'
import './ScenePanel'
import './TimeSeriesPanel'
import './StatePanel'
