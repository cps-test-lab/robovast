import Alert from '@mui/material/Alert'

// What the service says about a query's answer: the runs a table is missing or incomplete for,
// or a truncation it did not make for the row cap. Shown beside the rows, since either makes
// them less than the question asked for.
export function ResultNote({ note }: { note?: string | null }) {
  if (!note) return null
  return (
    <Alert severity="warning" variant="outlined" sx={{ py: 0 }}>
      {note}
    </Alert>
  )
}
