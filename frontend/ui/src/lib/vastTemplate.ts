// Starter content for a brand-new .vast file created from the Config editor. It is intentionally
// minimal but structurally complete (version + one configuration + an execution block), so the
// Monaco schema validation has something to guide the user from. Modeled on the real example
// files under metamorphic_testing/.

export const MINIMAL_VAST = `version: 1
configuration:
  - name: my-configuration
    variations: []
execution:
  image: ghcr.io/cps-test-lab/robovast:latest
  runs: 1
  scenario_file: scenario.osc
`
