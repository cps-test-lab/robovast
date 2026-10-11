// A run switch builds a new provider, and /describe is the campaign's rather than the run's -- so the
// providers of one campaign must share one answer instead of each asking again. Wired the way RunView
// wires it: each provider reads describe through the query cache, under `describeQuery`.
import { QueryClient } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const describeCampaignData = vi.fn()
const queryCampaignDataSql = vi.fn()
vi.mock('@/lib/robovastClient', () => ({ robovast: { describeCampaignData, queryCampaignDataSql } }))

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
    describeCampaignData.mockRejectedValueOnce(new Error('no campaign'))
    const [a, b] = providers(client)
    await expect(a.has('poses')).rejects.toThrow('no campaign')
    expect(await b.has('poses')).toBe(true)
    expect(describeCampaignData).toHaveBeenCalledTimes(2)
  })
})

// A table is built for a run the first time a query names it, and /describe lists its columns only
// once it is built for some run -- so an unbuilt table must not read as one missing every column.
describe('a table not built yet', () => {
  beforeEach(() => {
    describeCampaignData.mockReset()
    queryCampaignDataSql.mockReset()
    describeCampaignData.mockResolvedValue({ tables: [{ table: 'poses', columns: [] }] })
    queryCampaignDataSql.mockResolvedValue({ columns: ['timestamp', 'x'], rows: [] })
  })

  it('asks the run for its columns with an empty page scoped to that run', async () => {
    const [p] = providers(new QueryClient())
    expect(await p.has('poses', ['x'])).toBe(true)
    expect(await p.has('poses', ['y'])).toBe(false)
    expect(queryCampaignDataSql).toHaveBeenCalledWith(
      'c', `SELECT * FROM "poses" WHERE config_name = 'cfg' AND run_id = 0 LIMIT 0`, 1)
  })

  it('needs no query to say the table exists', async () => {
    const [p] = providers(new QueryClient())
    expect(await p.has('poses')).toBe(true)
    expect(queryCampaignDataSql).not.toHaveBeenCalled()
  })
})
