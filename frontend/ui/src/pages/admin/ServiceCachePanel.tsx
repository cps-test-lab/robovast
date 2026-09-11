import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { useToasts } from '@/components/ToastProvider'
import { formatBytes } from '@/lib/format'
import { robovast } from '@/lib/robovastClient'
import { clearableBytes } from './serviceCache'

/**
 * The service's rebuildable caches: what they hold, and a button that frees what may go.
 *
 * Measured when the panel opens rather than polled -- it walks every cached file, which is not
 * a question to ask every few seconds -- and replaced by the clear's own answer afterwards.
 */
export function ServiceCachePanel() {
  const queryClient = useQueryClient()
  const { notify } = useToasts()
  const [clearing, setClearing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const cache = useQuery({
    queryKey: ['serviceCache'],
    queryFn: robovast.serviceCache,
    retry: false,
  })

  if (!cache.isSuccess) {
    if (cache.isError) return <Alert severity="error">{(cache.error as Error).message}</Alert>
    return (
      <Typography variant="caption" color="text.disabled">
        measuring…
      </Typography>
    )
  }

  const report = cache.data
  const clearable = clearableBytes(report)

  const clear = async () => {
    setClearing(true)
    setError(null)
    try {
      const result = await robovast.clearServiceCache()
      queryClient.setQueryData(['serviceCache'], result)
      // The disk meter is what a user clears the cache to move.
      void queryClient.invalidateQueries({ queryKey: ['usage'] })
      notify({
        severity: 'success',
        message: `Freed ${formatBytes(result.freed_bytes)}`,
        note: `${result.removed_entries} cache entries removed.`,
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setClearing(false)
    }
  }

  return (
    <Stack spacing={1}>
      <Typography variant="caption" color="text.secondary">
        Copies of durable data — campaign files fetched from the object store, compiled 3D
        worlds. Clearing them loses nothing but the time to rebuild what is next asked for.
      </Typography>
      {report.caches.map((part) => (
        <Typography key={part.name} variant="caption">
          {part.name}: {formatBytes(part.size_bytes)} in {part.entries}{' '}
          {part.entries === 1 ? 'entry' : 'entries'}
        </Typography>
      ))}
      {report.kept.length ? (
        <Stack spacing={0.25}>
          <Typography variant="caption" color="text.secondary">
            Kept by a clear, because something may still be using them:
          </Typography>
          {report.kept.map((entry) => (
            <Typography
              key={`${entry.cache}/${entry.name}`}
              variant="caption"
              sx={{ fontFamily: 'monospace', wordBreak: 'break-all' }}
            >
              {entry.name} ({formatBytes(entry.size_bytes)}) — {entry.reason}
            </Typography>
          ))}
        </Stack>
      ) : null}
      {error ? <Alert severity="error">{error}</Alert> : null}
      <Box>
        <Button
          variant="outlined"
          size="small"
          disabled={clearing || clearable === 0}
          onClick={() => void clear()}
        >
          {clearing ? 'Clearing…' : `Clear cache (${formatBytes(clearable)})`}
        </Button>
      </Box>
    </Stack>
  )
}
