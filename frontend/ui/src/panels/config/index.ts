// Importing this module registers every built-in config-view panel (each panel file calls
// registerConfigPanel as a side effect). The Config page imports it once before parsing the vast's
// `visualization.config.panels`.
//
// Note: the `map2d` panel is not built in — it ships with the robovast_nav package as a
// Module-Federation remote and is loaded at runtime, the same way the run view's `costmap` is.
import './ParametersPanel'
import './WorldPanel'
import './Scene3DPanel'
