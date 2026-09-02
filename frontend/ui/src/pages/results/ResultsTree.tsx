import { useEffect, useMemo, useRef, useState, type SyntheticEvent } from 'react'
import { useQueries } from '@tanstack/react-query'
import { RichTreeView } from '@mui/x-tree-view/RichTreeView'
import {
  isPreviewable, robovast, RobovastError, type CampaignSummary,
} from '@/lib/robovastClient'
import { configDirs, previewRunRows, runIds } from '@/lib/previewRuns'
import { formatDataFetchLabel } from '@/lib/format'
import {
  ancestorIds,
  buildCampaignChildren,
  campaignItem,
  indexById,
  objectiveDirection,
  placeholderChild,
  CAMPAIGN_RUNS_MAX_ROWS,
  CAMPAIGN_RUNS_SQL,
  type ResultsTreeItem,
} from '@/lib/resultsTree'
import { StatusTreeItem } from './StatusTreeItem'

// One query per campaign feeds its whole subtree. Exported so the Run view's picker -- which
// renders this same tree -- reuses it verbatim: same key, so react-query serves both from one
// fetch, and the two surfaces cannot drift onto different rows for the same tree.
//
// Takes the campaign rather than its id because a campaign that is still RUNNING has no rows in the
// index to query -- they are written when postprocessing ingests it -- so its rows are derived from
// its output directories instead (`previewRuns.ts`). Both branches yield the same row shape, so
// everything downstream is unaware of which one answered.
//
// The mode is part of the KEY, which is what makes the transition safe in both directions: when the
// campaign finishes, the key changes and react-query fetches the indexed rows by itself. Sharing one
// key would instead serve the Explorer 60 s of verdict-less preview rows for a campaign that now has
// real verdicts, and require an invalidation somewhere that someone has to remember.
//
// Takes `undefined` for a campaign the caller does not have — a URL naming one this page never
// listed — because that is a real state at two of the three call sites and the query is disabled
// there anyway. The queryFn refuses rather than inventing an id: a disabled query never runs it, so
// reaching it means the `enabled` guard was dropped, and an empty result would hide that.
export function runsQuery(c: CampaignSummary | undefined) {
  const preview = !!c && isPreviewable(c)
  return {
    queryKey: ['runs', c?.campaign_id ?? '', preview ? 'preview' : 'indexed'],
    queryFn: () => {
      if (!c) throw new Error('runsQuery: no campaign to read runs for')
      return preview
        ? previewRows(c.campaign_id)
        : robovast.queryCampaignDataSql(
            c.campaign_id, CAMPAIGN_RUNS_SQL, CAMPAIGN_RUNS_MAX_ROWS)
    },
    retry: false,
    staleTime: 60_000,
  }
}

/** A directory that may not exist yet, listed. `null` for "not there", and only for that.
 *
 *  A campaign writes its configuration and run directories as it reaches them, so an address that
 *  is absent now is the ordinary first seconds of one -- and on the cluster, where a directory is a
 *  key prefix, absent means a 404 rather than an empty listing. Every OTHER failure is raised: an
 *  unreachable service and a campaign that has produced nothing look identical once both are an
 *  empty tree, and the one that needs saying is the one that would then never be said. */
async function listedOrAbsent(campaignId: string, path: string): Promise<string[] | null> {
  try {
    return (await robovast.listResultsDir(campaignId, path)).entries
  } catch (err) {
    if (err instanceof RobovastError && err.status === 404) return null
    throw err
  }
}

/** A running campaign's runs, from its output tree, in `queryCampaignDataSql`'s reply shape.
 *
 *  One listing for the configurations, then one per configuration for its runs -- so the cost is a
 *  few dozen requests carrying one short name per run, not the tens of thousands of paths a
 *  recursive listing of the same tree returns (it lists every FILE beneath it, rosbags included). */
async function previewRows(campaignId: string): Promise<{ rows: Record<string, unknown>[] }> {
  const configs = configDirs((await listedOrAbsent(campaignId, '')) ?? [])
  const runs = new Map<string, number[]>()
  await Promise.all(configs.map(async (name) => {
    const entries = await listedOrAbsent(campaignId, name)
    if (entries) runs.set(name, runIds(entries))
  }))
  return { rows: previewRunRows(runs) }
}

// The shared campaign → [batch →] config → run status tree (green/red, all from the DB). It is the
// campaign selector for both the Explorer (any node → its notebooks) and the Run view (a run leaf →
// replay). The batch level appears only for a search campaign, where a batch is one ask/tell round.
// Selection is controlled by the parent; `onSelect` fires with the clicked item (never a placeholder).
export function ResultsTree({
  campaigns,
  selectedId,
  onSelect,
}: {
  campaigns: CampaignSummary[]
  selectedId: string
  onSelect: (item: ResultsTreeItem) => void
}) {
  // Rendered in the order received — the service lists campaigns newest-first.
  const byId = useMemo(() => new Map(campaigns.map((c) => [c.campaign_id, c])), [campaigns])

  // Open on the selection: the Run view's dropdown remounts this tree on every open, so a tree that
  // started collapsed would hide the run it is showing. The ancestors are merged in rather than
  // assigned, so a hand-expanded branch survives a selection change.
  const [expandedItems, setExpandedItems] = useState<string[]>(() => ancestorIds(selectedId))
  useEffect(() => {
    const needed = ancestorIds(selectedId)
    setExpandedItems((prev) =>
      needed.every((id) => prev.includes(id)) ? prev : [...new Set([...prev, ...needed])],
    )
  }, [selectedId])

  // Lazy-load each expanded campaign's runs (config ids also land in expandedItems, so filter to
  // real campaigns). A single query per campaign feeds its whole subtree; `runsQuery` decides for
  // itself whether that campaign's rows come from the index or, while it is still running, from its
  // output directories.
  const expandedCampaigns = expandedItems.filter((id) => byId.has(id))
  const runQueries = useQueries({
    queries: expandedCampaigns.map((id) => runsQuery(byId.get(id))),
  })
  const runByCampaign = new Map(expandedCampaigns.map((id, i) => [id, runQueries[i]]))

  // Why the query above may be slow, asked in parallel with it. On a cluster campaign the
  // first `CAMPAIGN_RUNS_SQL` fetches the databases from the object store inside the request, so an
  // unexplained "Loading…" can sit there for minutes. Cheap (two metadata lookups, and none
  // at all while a transfer is running — the service then answers from memory) and advisory:
  // a failure just means the placeholder stays generic.
  //
  // Re-asked while the runs query is outstanding, because the answer is a live count: fetched
  // once, the placeholder would name the wait and then sit as still as the "Loading…" it
  // replaced. Once loaded it stops, so an idle tree polls nothing.
  //
  // Not asked at all for a campaign being previewed: it describes the transfer of databases that a
  // running campaign does not have yet, so the answer could only ever be "nothing to fetch" — and
  // it would be re-asked every second, per expanded campaign, against the service that is at that
  // moment driving the campaign.
  const statusQueries = useQueries({
    queries: expandedCampaigns.map((id, i) => ({
      queryKey: ['data-status', id],
      queryFn: () => robovast.campaignDataStatus(id),
      retry: false,
      staleTime: 60_000,
      enabled: !isPreviewable(byId.get(id)!),
      refetchInterval: runQueries[i]?.isFetching ? 1000 : false,
    })),
  })
  const statusByCampaign = new Map(expandedCampaigns.map((id, i) => [id, statusQueries[i]]))

  // Every campaign carries children so its expand arrow shows; the children are the real subtree
  // once loaded, otherwise a single placeholder (loading / no-data hint).
  const items: ResultsTreeItem[] = campaigns.map((c) => {
    const base = campaignItem(c)
    // Every campaign here either is finished+postprocessed, so it has a queryable store, or is
    // being previewed while it runs, where the listing stands in for one. Either way there is a
    // source to load from; go straight to the loading / real-subtree path.
    let children: ResultsTreeItem[]
    const q = runByCampaign.get(c.campaign_id)
    if (!q || q.isPending) {
      const fetching = formatDataFetchLabel(statusByCampaign.get(c.campaign_id)?.data)
      children = [placeholderChild(c.campaign_id, fetching ?? 'Loading…')]
    } else if (q.isError) {
      const msg = (q.error as Error).message
      // `run_view` needs no measurements, only the ingested campaign record, so this is a campaign
      // with no store to read at all (a deleted result dir, an unreachable object store) — not one
      // whose metrics are merely absent, which is how a tree built from the postprocessed `runs`
      // table would read it. A campaign still running never reaches here: its rows come from the
      // listing, which answers an empty tree rather than failing.
      children = [
        placeholderChild(
          c.campaign_id,
          /campaign\.db/i.test(msg) ? 'No results recorded for this campaign' : msg,
        ),
      ]
    } else {
      // Only a search campaign gets the batch level: a batch-mode campaign has exactly one
      // batch, so grouping by it would add a node that says nothing.
      const built = buildCampaignChildren(c.campaign_id, q.data.rows, {
        grouped: c.mode === 'search',
        direction: objectiveDirection(q.data.rows),
      })
      children = built.length ? built : [placeholderChild(c.campaign_id, 'No runs recorded')]
    }
    return { ...base, children }
  })

  const itemsById = useMemo(() => indexById(items), [items])

  // Expanding is not enough when the selected run sits below the fold of a long campaign list.
  // Its node only exists once the campaign's runs have loaded, hence the presence guard.
  const rootRef = useRef<HTMLUListElement>(null)
  const selectedPresent = itemsById.has(selectedId)
  useEffect(() => {
    if (!selectedPresent) return
    // `aria-selected="true"` marks the node: the custom item slot styles selection through
    // TreeItem2Content's `status`, so no `Mui-selected` class is emitted to key off.
    rootRef.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' })
  }, [selectedId, selectedPresent])

  const handleItemClick = (_e: SyntheticEvent, itemId: string) => {
    const item = itemsById.get(itemId)
    if (item && item.kind !== 'placeholder') onSelect(item)
  }

  return (
    <RichTreeView
      ref={rootRef}
      items={items}
      slots={{ item: StatusTreeItem }}
      expandedItems={expandedItems}
      onExpandedItemsChange={(_e, ids) => setExpandedItems(ids)}
      selectedItems={selectedId || null}
      onItemClick={handleItemClick}
    />
  )
}
