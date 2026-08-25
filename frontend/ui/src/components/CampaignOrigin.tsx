import { HoverFacts } from '@/components/HoverFacts'
import { originFacts } from '@/lib/campaignOrigin'
import type { CampaignOrigin as Origin } from '@/lib/robovastClient'

/**
 * Wraps a campaign's name so hovering it says where the campaign came from.
 *
 * A wrapper rather than a label of its own: the origin answers a question *about* the
 * campaign a reader has already found by its name, so it costs a row of the card to show it
 * standing alone, and the name is the thing already on screen to aim at.
 *
 * Three decisions worth keeping (the rows themselves are in `lib/campaignOrigin`):
 *
 * **It adds no element and no styling.** The tooltip attaches directly to whatever it is
 * given, so the campaign title keeps the exact markup and layout it had before — no wrapper
 * span, no underline, nothing that moves by a pixel. The hover is additive or it is not
 * worth having.
 *
 * **Not a link.** The workspace named is a fact about the past: it may have been renamed,
 * edited or deleted since, and for an ingested campaign it may never have existed on this
 * deployment at all. Nothing here is clickable, because anything clickable would promise it
 * is still there — and a re-run does not relaunch from it either (it runs from the
 * campaign's own frozen `_config/`).
 *
 * **Children pass through untouched when the origin was not recorded.** Campaigns that ran
 * before this was kept genuinely have no origin, so their name renders exactly as it always
 * did. The `.vast` basename in their snapshot is not an origin: it says nothing about which
 * workspace.
 */
export function CampaignOrigin({
  origin,
  children,
}: {
  origin?: Origin | null
  children: React.ReactElement
}) {
  if (!origin) return children

  return (
    <HoverFacts title="Launched from" facts={originFacts(origin)}>
      {children}
    </HoverFacts>
  )
}
