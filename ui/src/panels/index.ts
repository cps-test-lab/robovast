// Importing this module registers every built-in run-view panel with the registry (each panel file
// calls registerPanel as a side effect). RunView imports it once before parsing the vast panels.
import './PlaybackPanel'
import './RunLogPanel'
// Note: the `costmap` panel is no longer built-in — it ships with the robovast_nav package as a
// Module-Federation remote (src/robovast_nav/web) and is loaded at runtime by PanelHost.
import './ScenarioTreePanel'
import './ScenePanel'
import './Scene3DPanel'
import './TimeSeriesPanel'
import './StatePanel'
import './VegaPanel'
