import Typography from '@mui/material/Typography'

import { HoverFacts, hoverTriggerSx } from '@/components/HoverFacts'
import { originFacts, originLabel } from '@/lib/campaignOrigin'
import type { CampaignOrigin as Origin } from '@/lib/robovastClient'

/**
 * Where a campaign's configuration came from.
 *
 * Three decisions worth keeping (the label and row logic itself is in `lib/campaignOrigin`):
 *
 * **Not a link.** The workspace named here is a fact about the past: it may have been
 * renamed, edited or deleted since, and for an ingested campaign it may never have existed
 * on this deployment at all. Nothing here is clickable, because anything clickable would
 * promise it is still there — and a re-run does not relaunch from it either (it runs from
 * the campaign's own frozen `_config/`).
 *
 * **Nothing at all when the origin was not recorded.** Campaigns that ran before this was
 * kept genuinely have no origin, and the `.vast` basename recoverable from their snapshot is
 * not one — it says nothing about which workspace. An absent label is the honest rendering;
 * one reading "unknown" would just be a row nobody can act on.
 *
 * **The label is the summary, the hover is the detail.** The label is what you scan a list
 * with, so it is short; the ids and the full path go in the hover, where they cost nothing
 * until asked for.
 */
export function CampaignOrigin({ origin }: { origin?: Origin | null }) {
  const label = originLabel(origin)
  if (!origin || !label) return null

  return (
    <HoverFacts title="Launched from" facts={originFacts(origin)}>
      <Typography variant="caption" color="text.secondary" noWrap sx={hoverTriggerSx}>
        {label}
      </Typography>
    </HoverFacts>
  )
}
