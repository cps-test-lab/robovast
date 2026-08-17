// Starter content for a brand-new .vast file created from the Config editor. It is intentionally
// minimal but structurally complete, so the Monaco schema validation has something to guide the
// user from -- and so it VALIDATES: this was `version: 1` with a top-level `execution.image`, a
// shape validate_config refuses outright, so the editor's own starter file failed the first check
// anyone ran on it.
//
// No image is named. The scenario container runs the framework image, resolved from the
// deployment's project; `image:` is for a container of your own (see docs/images.rst).

export const MINIMAL_VAST = `version: 2
configuration:
  - name: my-configuration
    variations: []
execution:
  containers:
    scenario: {}
  runs: 1
  scenario_file: scenario.osc
`
