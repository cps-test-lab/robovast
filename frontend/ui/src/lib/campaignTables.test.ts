import { afterEach, describe, expect, it, vi } from 'vitest'
import { BUILD_TABLES_MESSAGE, offersBuildTables } from './campaignTables'
import { robovast } from './robovastClient'

describe('offersBuildTables', () => {
  it('offers the build on a finished campaign', () => {
    expect(offersBuildTables('finished')).toBe(true)
  })

  // A running campaign's runs are still arriving, and a failed or stopped one is not the campaign
  // someone is about to analyse at length -- its tables still build on first use.
  it.each(['running', 'postprocessing', 'failed', 'stopped', 'crashed', undefined])(
    'does not offer it on %s', (phase) => {
      expect(offersBuildTables(phase)).toBe(false)
    })

  it('says the build is optional and where its progress goes', () => {
    expect(BUILD_TABLES_MESSAGE).toMatch(/not needed/)
    expect(BUILD_TABLES_MESSAGE).toMatch(/first time something names it/)
    expect(BUILD_TABLES_MESSAGE).toMatch(/TABLES section/)
  })
})

describe('the table calls', () => {
  afterEach(() => vi.unstubAllGlobals())

  function serve(body: unknown) {
    const fetch = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }))
    vi.stubGlobal('fetch', fetch)
    return fetch
  }

  it('builds every table when none are named', async () => {
    const fetch = serve({ ok: true, message: '' })
    await robovast.buildCampaignTables('camp/1')
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toMatch(/\/campaigns\/camp%2F1\/tables\/build$/)
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({ campaign_id: 'camp/1', tables: [] })
  })

  it('passes the tables it is given', async () => {
    const fetch = serve({ ok: true, message: '' })
    await robovast.buildCampaignTables('c', ['poses', 'run_log'])
    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(JSON.parse(String(init.body)).tables).toEqual(['poses', 'run_log'])
  })

  it('clears with a DELETE on the tables resource', async () => {
    const fetch = serve({ campaign_id: 'c', freed_bytes: 42 })
    expect(await robovast.clearCampaignTables('c')).toEqual({ campaign_id: 'c', freed_bytes: 42 })
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toMatch(/\/campaigns\/c\/tables$/)
    expect(init.method).toBe('DELETE')
  })
})
