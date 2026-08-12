// Monaco setup: bundle the editor + YAML workers (no CDN — works offline/under CSP), point
// @monaco-editor/react at the bundled monaco, and expose a helper to attach the .vast JSON Schema
// (from the service's get_config_schema) for completion + inline validation.
import { loader } from '@monaco-editor/react'
import * as monaco from 'monaco-editor'
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import { configureMonacoYaml } from 'monaco-yaml'
import yamlWorker from 'monaco-yaml/yaml.worker?worker'

self.MonacoEnvironment = {
  getWorker(_moduleId: string, label: string) {
    return label === 'yaml' ? new yamlWorker() : new editorWorker()
  },
}

loader.config({ monaco })

let schemaConfigured = false

/** Associate the .vast JSON Schema with all YAML models (completion, hover, inline validation). */
export function configureVastSchema(schema: object): void {
  configureMonacoYaml(monaco, {
    enableSchemaRequest: false,
    hover: true,
    completion: true,
    validate: true,
    schemas: [{ uri: 'inmemory://robovast/config.schema.json', fileMatch: ['*'], schema }],
  })
  schemaConfigured = true
}

export function isSchemaConfigured(): boolean {
  return schemaConfigured
}

export { monaco }
