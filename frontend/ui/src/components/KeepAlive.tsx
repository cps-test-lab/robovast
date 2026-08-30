import { useRef } from 'react'
import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import { ActiveViewProvider } from '@/lib/activeView'

// Preserves a view's state across navigation. The view is mounted lazily on its first activation and
// thereafter kept mounted but hidden while inactive — so local state (selected workspace/file, editor
// buffers, scroll, preview) survives switching away and back, instead of being discarded on unmount.
// Keeping a view mounted would also freeze its data, because React Query reads and stops reading
// on mount and unmount. So the flag is published to the subtree (`ActiveViewProvider`), and the
// queries that should follow the view rather than the component take `enabled` from it: state is
// what survives being hidden, data is not. See lib/activeView.tsx.
//
// Hidden with `visibility` and taken out of flow, **not** `display: none`. A display-none subtree has
// no boxes at all, so every child measures itself as 0×0 — which is a lie for a component that sizes
// itself from its container. MUI's DataGrid says so out loud ("the parent DOM element has an empty
// height") every time you leave the Data browser, even though its Paper is a fixed 420px; anything
// else measuring while hidden gets the same wrong answer silently. `visibility: hidden` keeps the
// boxes, so the measurements stay true, and still skips painting and (with pointerEvents) input.
//
// `position: absolute` is what keeps an inactive view from taking space in the flow; the parent
// supplies the containing block. `overflow: hidden` stops a tall hidden view from lengthening the
// page's scrollable area — it is out of sight, so it must be out of the scrollbar too.
export function KeepAlive({ active, children }: { active: boolean; children: ReactNode }) {
  const everActive = useRef(false)
  everActive.current ||= active
  if (!everActive.current) return null
  return (
    <Box
      // `visibility: hidden` is what takes the subtree out of the tab order and the
      // accessibility tree as well as off the screen — no `inert` needed (and it is not in
      // React 18's JSX types anyway).
      sx={{
        minWidth: 0,
        ...(active
          ? {}
          : {
              position: 'absolute',
              top: 0,
              left: 0,
              right: 0,
              height: 0,
              overflow: 'hidden',
              visibility: 'hidden',
              pointerEvents: 'none',
            }),
      }}
    >
      <ActiveViewProvider active={active}>{children}</ActiveViewProvider>
    </Box>
  )
}
