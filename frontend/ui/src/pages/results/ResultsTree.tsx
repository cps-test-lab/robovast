import { useEffect, useMemo, useRef, useState, type SyntheticEvent } from 'react'
import { useQueries } from '@tanstack/react-query'
import { RichTreeView } from '@mui/x-tree-view/RichTreeView'
import { robovast, type CampaignSummary } from '@/lib/robovastClient'
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
export function runsQuery(campaignId: string) {
  return {
    queryKey: ['runs', campaignId],
    queryFn: () =>
      robovast.queryCampaignDataSql(campaignId, CAMPAIGN_RUNS_SQL, CAMPAIGN_RUNS_MAX_ROWS),
    retry: false,
    staleTime: 60_000,
  }
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
  // real campaigns — all are finished+postprocessed here). A single query per campaign feeds its
  // whole subtree.
  const expandedCampaigns = expandedItems.filter((id) => byId.has(id))
  const runQueries = useQueries({ queries: expandedCampaigns.map(runsQuery) })
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
  const statusQueries = useQueries({
    queries: expandedCampaigns.map((id, i) => ({
      queryKey: ['data-status', id],
      queryFn: () => robovast.campaignDataStatus(id),
      retry: false,
      staleTime: 60_000,
      refetchInterval: runQueries[i]?.isFetching ? 1000 : false,
    })),
  })
  const statusByCampaign = new Map(expandedCampaigns.map((id, i) => [id, statusQueries[i]]))

  // Every campaign carries children so its expand arrow shows; the children are the real subtree
  // once loaded, otherwise a single placeholder (loading / no-data hint).
  const items: ResultsTreeItem[] = campaigns.map((c) => {
    const base = campaignItem(c)
    // Every campaign here is finished+postprocessed, so it always has a queryable store; go
    // straight to the loading / real-subtree path.
    let children: ResultsTreeItem[]
    const q = runByCampaign.get(c.campaign_id)
    if (!q || q.isPending) {
      const fetching = formatDataFetchLabel(statusByCampaign.get(c.campaign_id)?.data)
      children = [placeholderChild(c.campaign_id, fetching ?? 'Loading…')]
    } else if (q.isError) {
      const msg = (q.error as Error).message
      // `run_view` needs only `campaign.db`, so this is a campaign with no store to read at all
      // (a deleted result dir, an unreachable object store) — not one still awaiting
      // postprocessing, which is how a tree built from `data.db`'s `runs` would read it.
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
