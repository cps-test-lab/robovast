import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import type { PanelSpec } from '@robovast/panel-kit'
import { runViewHashOf } from '@/lib/jobRun'
import JobRunScene from './JobRunScene'

const spec = { id: 'scene', type: 'scene3d', config: {} } as unknown as PanelSpec

function render(): string {
  return renderToStaticMarkup(
    <QueryClientProvider client={new QueryClient()}>
      <JobRunScene
        campaignId="c1"
        run={{ configName: 'cfg-a', runId: 3 }}
        spec={spec}
        timeline={null}
        onClose={() => {}}
      />
    </QueryClientProvider>,
  )
}

describe('JobRunScene', () => {
  it('has no title row: the run is named only by the controls over the scene', () => {
    const html = render()
    expect(html).not.toContain('3D ·')
    expect(html).not.toMatch(/>[^<]*cfg-a[^<]*</)
  })

  it('floats its controls in the top-right corner, each with a label naming what it does', () => {
    const html = render()
    const controls = /<div[^>]*data-scene-controls[^>]*>/.exec(html)?.[0] ?? ''
    expect(controls).toMatch(/position:absolute/)
    expect(controls).toMatch(/top:8px/)
    expect(controls).toMatch(/right:8px/)
    expect(html).toContain('aria-label="open cfg-a run 3 in run view"')
    expect(html).toContain(`href="${runViewHashOf('c1', { configName: 'cfg-a', runId: 3 })}"`)
    expect(html).toContain('aria-label="reset 3D view"')
    expect(html).toContain('aria-label="hide 3D view"')
  })
})
