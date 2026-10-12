import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { LogBesidePane } from './JobLogWithScene'

/** The inline height of every pane, by its `data-pane` name. */
function paneHeights(html: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const m of html.matchAll(/<div[^>]*data-pane="(\w+)"[^>]*>/g)) {
    out[m[1]] = /style="height:([^"]+)"/.exec(m[0])?.[1] ?? ''
  }
  return out
}

describe('LogBesidePane', () => {
  it('puts the log and the 3D view side by side at the same height', () => {
    const html = renderToStaticMarkup(
      <LogBesidePane height={320} log={<span>LOG</span>} side={<span>SCENE</span>} />,
    )
    expect(paneHeights(html)).toEqual({ log: '320px', side: '320px' })
    expect(html.indexOf('LOG')).toBeLessThan(html.indexOf('SCENE'))
  })

  it('gives the log the whole row when there is no 3D view', () => {
    const html = renderToStaticMarkup(
      <LogBesidePane height={320} log={<span>LOG</span>} side={null} />,
    )
    expect(paneHeights(html)).toEqual({ log: '320px' })
  })
})
