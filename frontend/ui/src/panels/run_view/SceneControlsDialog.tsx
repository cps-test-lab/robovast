// What moves the 3D view, for mouse and for fingers: the answer to "how do I move this?" from the run
// view's gear, where the way back (Reset 3D view) already is. The bindings themselves live in
// lib/scene3d (viewport.ts and touchNavigation.ts); this only says them.

import Dialog from '@mui/material/Dialog'
import DialogContent from '@mui/material/DialogContent'
import DialogTitle from '@mui/material/DialogTitle'
import Table from '@mui/material/Table'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableRow from '@mui/material/TableRow'
import Typography from '@mui/material/Typography'
import useMediaQuery from '@mui/material/useMediaQuery'

const MOUSE: [string, string][] = [
  ['Wheel', 'Fly toward or away from what is under the pointer.'],
  ['Left-drag', 'Turn about the surface under the pointer.'],
  ['Right-drag', 'Pan sideways and up or down.'],
  ['Double-click', 'Fly to frame what was clicked.'],
]

const TOUCH: [string, string][] = [
  ['One finger', 'Turn about the surface under the finger.'],
  ['Pinch', 'Fly toward or away from what is between the fingers.'],
  ['Two fingers, together', 'Pan sideways and up or down.'],
  ['Two fingers, turning', 'Turn the view in place.'],
  ['Double tap', 'Fly to frame what was tapped.'],
]

function Bindings({ title, rows }: { title: string; rows: [string, string][] }) {
  return (
    <>
      <Typography variant="subtitle2" sx={{ mt: 1 }}>
        {title}
      </Typography>
      <Table size="small">
        <TableBody>
          {rows.map(([gesture, effect]) => (
            <TableRow key={gesture}>
              <TableCell sx={{ whiteSpace: 'nowrap', fontWeight: 600, pl: 0 }}>{gesture}</TableCell>
              <TableCell>{effect}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </>
  )
}

export function SceneControlsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  // Whichever this device is driven by comes first; both are listed, since a laptop can have a
  // touch screen and a tablet a mouse.
  const touchFirst = useMediaQuery('(pointer: coarse)')
  const mouse = <Bindings key="mouse" title="Mouse" rows={MOUSE} />
  const touch = <Bindings key="touch" title="Touch" rows={TOUCH} />
  return (
    <Dialog open={open} onClose={onClose} maxWidth="xs" fullWidth>
      <DialogTitle>Moving the 3D view</DialogTitle>
      <DialogContent>
        {touchFirst ? [touch, mouse] : [mouse, touch]}
        <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
          Lost the scene? <b>Reset 3D view</b> in this menu puts the camera back where it opened.
        </Typography>
      </DialogContent>
    </Dialog>
  )
}
