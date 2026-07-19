import { useRef } from 'react'
import type { ReactNode } from 'react'
import Box from '@mui/material/Box'

// Preserves a view's state across navigation. The view is mounted lazily on its first activation and
// thereafter kept mounted but hidden (display:none) while inactive — so local state (selected
// workspace/file, editor buffers, scroll, preview) survives switching away and back, instead of being
// discarded on unmount. Inactive views don't render visibly but their queries stay live.
export function KeepAlive({ active, children }: { active: boolean; children: ReactNode }) {
  const everActive = useRef(false)
  everActive.current ||= active
  if (!everActive.current) return null
  return <Box sx={{ display: active ? 'block' : 'none', minWidth: 0 }}>{children}</Box>
}
