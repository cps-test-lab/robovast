// Starter content for a brand-new .vast file created from the Config editor. It is intentionally
// minimal but structurally complete, so the Monaco schema validation has something to guide the
// user from -- and so it VALIDATES: `tests/common/test_new_file_template.py` validates this body
// under the version the config schema publishes.
//
// No image is named. The scenario container runs the framework image, resolved from the
// deployment's project; `image:` is for a container of your own (see docs/images.rst).

/** Everything below the `version:` line. */
export const VAST_BODY = `configuration:
  - name: my-configuration
    variations: []
execution:
  containers:
    scenario: {}
  runs: 1
  scenario_file: scenario.osc
`

/** A new .vast, declaring the config version the service accepts: the default of `version` in
 *  the config schema it publishes, so the service is the one place that number lives. Throws
 *  when the schema carries no integer default, rather than writing a file that cannot pass. */
export function minimalVast(schema: Record<string, unknown>): string {
  const properties = schema.properties as Record<string, { default?: unknown }> | undefined
  const version = properties?.version?.default
  if (!Number.isInteger(version))
    throw new Error('The config schema names no default version, so a new .vast cannot declare one.')
  return `version: ${version}\n${VAST_BODY}`
}
