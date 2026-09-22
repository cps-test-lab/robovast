// A run switch builds a new provider, and /describe is the campaign's rather than the run's -- so the
// providers of one campaign must share one answer instead of each asking again. Wired the way RunView
// wires it: each provider reads describe through the query cache, under `describeQuery`.
import { QueryClient } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const describeCampaignData = vi.fn()
vi.mock('@/lib/robovastClient', () => ({ robovast: { describeCampaignData } }))

const { dbDataProvider, describeQuery } = await import('./dataProvider')

const TABLES = { tables: [{ table: 'poses', columns: ['timestamp REAL', 'x REAL'] }] }

function providers(client: QueryClient, version = 'v1') {
  const getDescribe = () => client.fetchQuery(describeQuery('c', version))
  return [0, 1, 2].map((run) => dbDataProvider('c', 'cfg', run, getDescribe))
}

describe('describe is per campaign', () => {
  beforeEach(() => {
    describeCampaignData.mockReset()
    describeCampaignData.mockResolvedValue(TABLES)
  })

  it('is fetched once for every provider of the campaign', async () => {
    const client = new QueryClient()
    for (const p of providers(client)) {
      expect(await p.has('poses', ['x'])).toBe(true)
      expect(await p.has('behaviors')).toBe(false)
    }
    expect(describeCampaignData).toHaveBeenCalledTimes(1)
  })

  it('is asked again under a new version, and after the Data browser invalidates it', async () => {
    const client = new QueryClient()
    await providers(client, 'v1')[0].has('poses')
    await providers(client, 'v2')[0].has('poses')
    expect(describeCampaignData).toHaveBeenCalledTimes(2)

    // The Data browser's own key, without a version: a prefix of this one.
    await client.invalidateQueries({ queryKey: ['describe', 'c'] })
    await providers(client, 'v2')[1].has('poses')
    expect(describeCampaignData).toHaveBeenCalledTimes(3)
  })

  it('does not keep a failure: the next reader asks again', async () => {
    const client = new QueryClient()
    describeCampaignData.mockRejectedValueOnce(new Error('not in the index'))
    const [a, b] = providers(client)
    await expect(a.has('poses')).rejects.toThrow('not in the index')
    expect(await b.has('poses')).toBe(true)
    expect(describeCampaignData).toHaveBeenCalledTimes(2)
  })
})
