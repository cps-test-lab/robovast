import { useMemo, useState, type SyntheticEvent } from 'react'
import { useQueries } from '@tanstack/react-query'
import { RichTreeView } from '@mui/x-tree-view/RichTreeView'
import { robovast, campaignsNewestFirst, type CampaignSummary } from '@/lib/robovastClient'
import {
  buildCampaignChildren,
  campaignItem,
  indexById,
  placeholderChild,
  type ResultsTreeItem,
} from '@/lib/resultsTree'
import { StatusTreeItem } from './StatusTreeItem'

// The per-run breakdown lives only in data.db; pull the pass/fail matrix in one query per campaign.
const RUNS_SQL = 'SELECT config_name, run_id, status, passed FROM runs ORDER BY config_name, run_id'

// The shared campaign → config → run status tree (green/red, all from the DB). It is the campaign
// selector for both the Explorer (any node → its notebooks) and the Run view (a run leaf → replay).
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
  const sorted = useMemo(() => campaignsNewestFirst(campaigns), [campaigns])
  const byId = useMemo(() => new Map(sorted.map((c) => [c.campaign_id, c])), [sorted])

  const [expandedItems, setExpandedItems] = useState<string[]>([])

  // Lazy-load each expanded campaign's runs (config ids also land in expandedItems, so filter to
  // real, postprocessed campaigns). A single query per campaign feeds its whole subtree.
  const expandedCampaigns = expandedItems.filter((id) => byId.get(id)?.postprocessed)
  const runQueries = useQueries({
    queries: expandedCampaigns.map((id) => ({
      queryKey: ['runs', id],
      queryFn: () => robovast.queryCampaignDataSql(id, RUNS_SQL),
      retry: false,
      staleTime: 60_000,
    })),
  })
  const runByCampaign = new Map(expandedCampaigns.map((id, i) => [id, runQueries[i]]))

  // Every campaign carries children so its expand arrow shows; the children are the real subtree
  // once loaded, otherwise a single placeholder (loading / no-data hint).
  const items: ResultsTreeItem[] = sorted.map((c) => {
    const base = campaignItem(c)
    let children: ResultsTreeItem[]
    if (!c.postprocessed) {
      children = [placeholderChild(c.campaign_id, 'No results yet — run postprocessing')]
    } else {
      const q = runByCampaign.get(c.campaign_id)
      if (!q || q.isPending) {
        children = [placeholderChild(c.campaign_id, 'Loading…')]
      } else if (q.isError) {
        const msg = (q.error as Error).message
        children = [
          placeholderChild(
            c.campaign_id,
            /data\.db/i.test(msg) ? 'No results yet — run postprocessing' : msg,
          ),
        ]
      } else {
        const built = buildCampaignChildren(c.campaign_id, q.data.rows)
        children = built.length ? built : [placeholderChild(c.campaign_id, 'No runs recorded')]
      }
    }
    return { ...base, children }
  })

  const itemsById = useMemo(() => indexById(items), [items])

  const handleItemClick = (_e: SyntheticEvent, itemId: string) => {
    const item = itemsById.get(itemId)
    if (item && item.kind !== 'placeholder') onSelect(item)
  }

  return (
    <RichTreeView
      items={items}
      slots={{ item: StatusTreeItem }}
      expandedItems={expandedItems}
      onExpandedItemsChange={(_e, ids) => setExpandedItems(ids)}
      selectedItems={selectedId || null}
      onItemClick={handleItemClick}
    />
  )
}
