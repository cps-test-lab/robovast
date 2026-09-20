// The "Preview" badge and the one explanation of what a preview is.
//
// Written once because it is shown in two views that must not describe the state differently: a
// reader who sees "Preview" in the Run view and again in the Explorer is looking at one campaign in
// one condition. The wording therefore names the condition and the remedy, not the view.

import { Chip, Tooltip } from '@mui/material'

/** What a preview can and cannot show, in the terms a reader asks it in. */
export const PREVIEW_EXPLANATION =
  'This campaign is still running. Its finished runs replay in 3D from their own recordings and '
  + 'their logs are read from what each run wrote; metrics, charts and pass/fail need '
  + 'postprocessing, which happens when the campaign ends. A run still in progress is listed but '
  + 'has nothing to replay yet.'

export function PreviewChip() {
  return (
    <Tooltip title={PREVIEW_EXPLANATION}>
      <Chip size="small" color="warning" label="Preview" sx={{ pointerEvents: 'auto' }} />
    </Tooltip>
  )
}
