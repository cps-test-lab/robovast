import Box from '@mui/material/Box'
import type { DataTable } from '@/lib/robovastClient'
import { tableFailures } from '@/lib/dataTables'

// The runs a described table is missing or incomplete for, each with its reason, under the
// table's entry in the Data browser's schema panel. describe carries at most a sample of them.
export function TableFailures({ table }: { table: DataTable }) {
  const failures = tableFailures(table)
  if (!failures.length) return null
  return (
    <Box
      component="ul"
      aria-label={`${table.table}: runs missing or incomplete`}
      sx={{ m: 0, pl: 2, color: 'warning.main', fontSize: 11 }}
    >
      {failures.map(([run, reason]) => (
        <li key={run}>
          <Box component="span" sx={{ fontFamily: 'monospace' }}>{run}</Box>: {reason}
        </li>
      ))}
    </Box>
  )
}
