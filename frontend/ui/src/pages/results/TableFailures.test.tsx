import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { DataTable } from '@/lib/robovastClient'
import { TableFailures } from './TableFailures'

const table = (over: Partial<DataTable>): DataTable => ({
  schema: 'main', table: 'rosbag2_torn', columns: [], rows: 1, kind: 'table', runs: 2, built: 1,
  failed: {}, description: '', column_notes: {}, ...over,
})

describe('TableFailures', () => {
  it('renders nothing for a table every run is built for whole', () => {
    expect(renderToStaticMarkup(<TableFailures table={table({})} />)).toBe('')
  })

  // A build that failed and a table cut short by an undecodable topic are both a run under
  // describe's `failed`, each shown with its own reason.
  it('lists each run the table is missing or incomplete for, with its reason', () => {
    const html = renderToStaticMarkup(<TableFailures table={table({
      failed: {
        'c/cfg/1': 'topic /torn (std_msgs/msg/String) is undecodable: a message does not decode',
        'c/cfg/0': 'HandlerError: no frames',
      },
    })} />)
    // Emotion inlines each styled element's <style> in server markup: only the text is read.
    const items = [...html.matchAll(/<li>(.*?)<\/li>/g)]
      .map((m) => m[1].replace(/<style[^>]*>.*?<\/style>/g, '').replace(/<[^>]+>/g, ''))
    expect(items).toEqual([
      'c/cfg/0: HandlerError: no frames',
      'c/cfg/1: topic /torn (std_msgs/msg/String) is undecodable: a message does not decode',
    ])
    expect(html).toContain('aria-label="rosbag2_torn: runs missing or incomplete"')
  })
})
